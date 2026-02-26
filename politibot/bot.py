"""
bot.py
Hoved-orkestrator for PolitiBot.

Kjører en syklus:
1. Hent nye politikerhandler
2. Score og ranger dem
3. Utfør topp-signaler
4. Overvåk åpne posisjoner
5. Logg og rapporter
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from data.fetcher import PoliticianTradesFetcher
from scoring.engine import SignalEngine, TradeSignal
from execution.trader import PolitibotTrader

logger = logging.getLogger(__name__)

DEFAULT_CFG = {
    "initial_capital":   100_000,
    "paper":             True,
    "check_interval":    3600,    # Sjekk nye handler hvert 60. minutt
    "days_lookback":     30,      # Hent handler siste 30 dager ved oppstart
    "max_signals_per_run": 3,     # Maks 3 nye posisjoner per kjøring
    "log_dir":           "logs",
    "alpaca_api_key":    "",
    "alpaca_secret_key": "",
}


class PolitiBot:
    """
    PolitiBot — kopier de smarte pengene.
    
    ⚠️ Ansvarsfraskrivelse: Politikerhandler er offentlig info, 
    men 45-dagers forsinkelse betyr at signalene er historiske.
    Ingen garanti for profitt. Alt skjer på eget ansvar.
    """

    def __init__(self, cfg: dict = None):
        self.cfg = {**DEFAULT_CFG, **(cfg or {})}
        Path(self.cfg["log_dir"]).mkdir(parents=True, exist_ok=True)
        self._setup_logging()

        self.fetcher = PoliticianTradesFetcher()
        self.signal_engine = SignalEngine()
        self.trader = PolitibotTrader(self.cfg, paper=self.cfg["paper"])
        self._seen_trade_ids: set[str] = set()
        self._all_signals: list[TradeSignal] = []

    def start(self) -> None:
        """Start botens hoved-loop."""
        logger.info("=" * 60)
        logger.info("🇺🇸 PolitiBot starter")
        logger.info(f"   Modus: {'PAPER' if self.cfg['paper'] else '🔴 LIVE'}")
        logger.info(f"   Kapital: ${self.cfg['initial_capital']:,}")
        logger.info("=" * 60)

        self.trader.connect()

        # Første kjøring: last historiske data
        logger.info("Henter historiske politikerhandler...")
        all_history = self.fetcher.fetch_all(days_back=self.cfg["days_lookback"])
        self._process_batch(all_history, all_history)

        # Hoved-loop
        while True:
            try:
                logger.info(f"\n⏰ Neste sjekk om {self.cfg['check_interval']//60} min...")
                time.sleep(self.cfg["check_interval"])

                logger.info("🔍 Sjekker nye handler...")
                recent = self.fetcher.fetch_recent(days=2)
                new_trades = [t for t in recent if t.trade_id not in self._seen_trade_ids]

                if new_trades:
                    logger.info(f"📬 {len(new_trades)} nye handler funnet!")
                    self._process_batch(new_trades, all_history + recent)
                    all_history = (all_history + recent)[-5000:]  # Behold siste 5000
                else:
                    logger.info("Ingen nye handler funnet.")

                # Oppdater åpne posisjoner
                self._update_positions()

            except KeyboardInterrupt:
                logger.info("\n⛔ Stopper PolitiBot...")
                self._print_final_report()
                break
            except Exception as e:
                logger.error(f"Feil i hoved-loop: {e}", exc_info=True)
                time.sleep(60)

    def run_once(self, days_back: int = 60) -> list[TradeSignal]:
        """Kjør én gang — for testing eller manuell bruk."""
        self.trader.connect()
        all_trades = self.fetcher.fetch_all(days_back=days_back)
        recent = self.fetcher.fetch_recent(days=30)
        return self._process_batch(recent, all_trades, execute=False)

    def _process_batch(
        self,
        trades_to_score: list,
        all_history: list,
        execute: bool = True,
    ) -> list[TradeSignal]:
        """Score og optionelt utfør en batch med handler."""
        if not trades_to_score:
            return []

        signals = self.signal_engine.generate_signals(trades_to_score, all_history)
        self._all_signals.extend(signals)

        logger.info(f"\n{'─'*50}")
        logger.info(f"📊 {len(signals)} signaler generert")

        # Vis topp-signaler
        for sig in signals[:10]:
            icon = "🚨" if sig.total_score >= 80 else "📈" if sig.total_score >= 65 else "👀"
            logger.info(
                f"{icon} [{sig.recommendation:10s}] {sig.trade.symbol:6s} "
                f"score={sig.total_score:.0f}/100 | {sig.trade.politician}"
            )
            for reason in sig.reasons[:3]:
                logger.info(f"   → {reason}")

        # Utfør topp-signaler
        if execute:
            executed = 0
            for sig in signals:
                if executed >= self.cfg["max_signals_per_run"]:
                    break
                if sig.recommendation in ("STRONG BUY", "BUY"):
                    trade = self.trader.execute_signal(sig)
                    if trade:
                        executed += 1
                        self._seen_trade_ids.add(sig.trade.trade_id)
                        self._log_signal(sig, trade)

        self._save_signals(signals)
        return signals

    def _update_positions(self) -> None:
        """Hent siste priser og sjekk stops."""
        if not self.trader._positions:
            return

        symbols = list(self.trader._positions.keys())
        prices = {}

        try:
            import yfinance as yf
            for sym in symbols:
                hist = yf.Ticker(sym).history(period="1d")
                if not hist.empty:
                    prices[sym] = float(hist["Close"].iloc[-1])
        except Exception as e:
            logger.error(f"Klarte ikke hente priser: {e}")
            return

        closed = self.trader.update_positions(prices)
        if closed:
            logger.info(f"Lukkede posisjoner: {closed}")

        summary = self.trader.portfolio_summary(prices)
        logger.info(
            f"📈 Portefølje: Egenkapital=${summary['total_equity']:,} | "
            f"Åpen PnL=${summary['open_pnl']:+,.2f} | "
            f"Lukket PnL=${summary['closed_pnl']:+,.2f}"
        )

    def _print_final_report(self) -> None:
        """Skriv ut sluttrapport."""
        try:
            import yfinance as yf
            prices = {
                sym: float(yf.Ticker(sym).history(period="1d")["Close"].iloc[-1])
                for sym in self.trader._positions
            }
        except:
            prices = {}

        summary = self.trader.portfolio_summary(prices)
        print("\n" + "="*60)
        print("  SLUTTRAPPORT — PolitiBot")
        print("="*60)
        print(f"  Egenkapital: ${summary['total_equity']:,}")
        print(f"  Lukket PnL:  ${summary['closed_pnl']:+,.2f}")
        print(f"  Åpen PnL:    ${summary['open_pnl']:+,.2f}")
        print(f"  Win rate:    {summary['win_rate']}")
        print(f"  Antall handler: {summary['total_trades']}")
        print("="*60)

    def _log_signal(self, sig: TradeSignal, trade: Any) -> None:
        log_file = Path(self.cfg["log_dir"]) / "executed_trades.jsonl"
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": sig.trade.symbol,
            "politician": sig.trade.politician,
            "score": sig.total_score,
            "recommendation": sig.recommendation,
            "shares": trade.shares,
            "entry_price": trade.entry_price,
            "stop_loss": trade.stop_loss,
            "take_profit": trade.take_profit,
            "reasons": sig.reasons,
        }
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _save_signals(self, signals: list[TradeSignal]) -> None:
        out = Path(self.cfg["log_dir"]) / f"signals_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        data = [
            {
                "symbol": s.trade.symbol,
                "politician": s.trade.politician,
                "score": s.total_score,
                "recommendation": s.recommendation,
                "urgency": s.urgency,
                "transaction_date": str(s.trade.transaction_date),
                "disclosure_date": str(s.trade.disclosure_date),
                "delay_days": s.trade.filing_delay_days,
                "amount": s.trade.avg_amount,
                "is_option": s.trade.is_option,
                "committee": s.trade.committee,
                "reasons": s.reasons,
            }
            for s in signals
        ]
        with open(out, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Signaler lagret: {out}")

    def _setup_logging(self) -> None:
        log_file = Path(self.cfg["log_dir"]) / f"politibot_{datetime.now().strftime('%Y%m%d')}.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.StreamHandler(), logging.FileHandler(log_file)],
        )
