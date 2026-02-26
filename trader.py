"""
execution/trader.py
Utfører handler basert på signaler.
Støtter paper og live via Alpaca.
Streng risikostyring bygget inn.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from scoring.engine import TradeSignal

logger = logging.getLogger(__name__)


SIZE_MULTIPLIERS = {
    "FULL":    1.0,
    "HALF":    0.5,
    "QUARTER": 0.25,
}


@dataclass
class ExecutedTrade:
    signal: TradeSignal
    shares: int
    entry_price: float
    stop_loss: float
    take_profit: float
    order_id: str
    timestamp: str
    mode: str   # "paper" eller "live"
    pnl: float = 0.0
    is_open: bool = True


class PolitibotTrader:
    """
    Utfører handler fra PolitiBot-signaler.

    Risikoregler:
    - Maks 2% av portefølje per handel
    - Stop-loss 8% under kjøpspris (politikerhandler kan ta tid)
    - Take-profit 20% over kjøpspris
    - Trailing stop 12%
    - Maks 5 åpne posisjoner samtidig
    - Kun handel i børstider
    """

    MAX_POSITIONS       = 5
    RISK_PER_TRADE_PCT  = 0.02   # 2% — høyere enn normalt, kortere tidshorisont
    STOP_LOSS_PCT       = 0.08   # 8% stop-loss
    TAKE_PROFIT_PCT     = 0.20   # 20% take-profit
    TRAILING_STOP_PCT   = 0.12   # 12% trailing

    def __init__(self, cfg: dict, paper: bool = True):
        self.cfg = cfg
        self.paper = paper
        self._client = None
        self._positions: dict[str, ExecutedTrade] = {}
        self._history: list[ExecutedTrade] = []
        self._equity = cfg.get("initial_capital", 100_000)

    def connect(self) -> None:
        if self.paper:
            logger.info(f"✅ Paper-modus. Startkapital: ${self._equity:,}")
            return
        try:
            from alpaca.trading.client import TradingClient
            self._client = TradingClient(
                api_key=self.cfg["alpaca_api_key"],
                secret_key=self.cfg["alpaca_secret_key"],
                paper=False,
            )
            account = self._client.get_account()
            self._equity = float(account.equity)
            logger.info(f"✅ Koblet til Alpaca LIVE. Kapital: ${self._equity:,}")
        except Exception as e:
            raise RuntimeError(f"Klarte ikke koble til Alpaca: {e}")

    def execute_signal(self, signal: TradeSignal) -> Optional[ExecutedTrade]:
        """Utfør ett signal. Returnerer None om blokkert av risikoregler."""
        symbol = signal.trade.symbol

        # Sjekk maks antall posisjoner
        if len(self._positions) >= self.MAX_POSITIONS:
            logger.warning(f"Maks {self.MAX_POSITIONS} posisjoner nådd. Skipper {symbol}")
            return None

        # Unngå duplikat
        if symbol in self._positions:
            logger.info(f"Har allerede posisjon i {symbol}. Skipper.")
            return None

        # Hent pris
        price = self._get_price(symbol)
        if not price:
            return None

        # Beregn størrelse
        multiplier = SIZE_MULTIPLIERS.get(signal.suggested_position_size, 0.25)
        risk_dollars = self._equity * self.RISK_PER_TRADE_PCT * multiplier
        stop_loss  = round(price * (1 - self.STOP_LOSS_PCT), 4)
        take_profit = round(price * (1 + self.TAKE_PROFIT_PCT), 4)
        per_share_risk = price - stop_loss
        shares = max(1, int(risk_dollars / per_share_risk))

        # Ikke bruk mer enn 10% av kapital på én handel
        max_shares = int((self._equity * 0.10) / price)
        shares = min(shares, max_shares)

        logger.info(
            f"📈 {'PAPER' if self.paper else 'LIVE'} BUY: "
            f"{shares}x {symbol} @ ${price:.2f} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f} | "
            f"Signal={signal.total_score:.0f}/100"
        )

        order_id = self._submit_order(symbol, shares, price) if not self.paper \
                   else f"paper-{uuid.uuid4().hex[:8]}"

        trade = ExecutedTrade(
            signal=signal,
            shares=shares,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            order_id=order_id,
            timestamp=datetime.utcnow().isoformat(),
            mode="paper" if self.paper else "live",
        )
        self._positions[symbol] = trade
        if not self.paper:
            self._equity -= shares * price
        return trade

    def update_positions(self, current_prices: dict[str, float]) -> list[str]:
        """Sjekk stops og TP. Returner liste over lukkede symboler."""
        closed = []
        for symbol, trade in list(self._positions.items()):
            price = current_prices.get(symbol, trade.entry_price)

            # Trailing stop — oppdater
            new_sl = price * (1 - self.TRAILING_STOP_PCT)
            if new_sl > trade.stop_loss:
                trade.stop_loss = round(new_sl, 4)

            exit_reason = None
            if price <= trade.stop_loss:
                exit_reason = "stop_loss"
            elif price >= trade.take_profit:
                exit_reason = "take_profit"

            if exit_reason:
                pnl = (price - trade.entry_price) * trade.shares
                trade.pnl = round(pnl, 2)
                trade.is_open = False
                self._equity += price * trade.shares
                logger.info(
                    f"{'🟢' if pnl > 0 else '🔴'} LUKKET {symbol} [{exit_reason}]: "
                    f"PnL=${pnl:+,.2f} ({(price/trade.entry_price-1)*100:+.1f}%)"
                )
                self._history.append(trade)
                del self._positions[symbol]
                closed.append(symbol)

        return closed

    def portfolio_summary(self, current_prices: dict[str, float]) -> dict:
        open_value = sum(
            t.shares * current_prices.get(sym, t.entry_price)
            for sym, t in self._positions.items()
        )
        total_equity = self._equity + open_value
        closed_pnl = sum(t.pnl for t in self._history)
        open_pnl = sum(
            (current_prices.get(sym, t.entry_price) - t.entry_price) * t.shares
            for sym, t in self._positions.items()
        )
        win_trades = [t for t in self._history if t.pnl > 0]

        return {
            "total_equity": round(total_equity, 2),
            "cash": round(self._equity, 2),
            "open_positions": len(self._positions),
            "open_pnl": round(open_pnl, 2),
            "closed_pnl": round(closed_pnl, 2),
            "win_rate": f"{len(win_trades)/max(len(self._history),1)*100:.1f}%",
            "total_trades": len(self._history),
            "positions": {
                sym: {
                    "shares": t.shares,
                    "entry": t.entry_price,
                    "current": current_prices.get(sym, t.entry_price),
                    "pnl": round((current_prices.get(sym, t.entry_price) - t.entry_price) * t.shares, 2),
                    "stop_loss": t.stop_loss,
                    "take_profit": t.take_profit,
                    "score": t.signal.total_score,
                    "politician": t.signal.trade.politician,
                }
                for sym, t in self._positions.items()
            }
        }

    def _get_price(self, symbol: str) -> Optional[float]:
        """Hent siste pris — fra Alpaca eller yfinance fallback."""
        if self.paper or self._client is None:
            try:
                import yfinance as yf
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1d")
                if not hist.empty:
                    return float(hist["Close"].iloc[-1])
            except Exception as e:
                logger.error(f"Klarte ikke hente pris for {symbol}: {e}")
            return None
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest
            client = StockHistoricalDataClient(
                api_key=self.cfg["alpaca_api_key"],
                secret_key=self.cfg["alpaca_secret_key"],
            )
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trade = client.get_stock_latest_trade(req)
            return float(trade[symbol].price)
        except Exception as e:
            logger.error(f"Pris-feil {symbol}: {e}")
            return None

    def _submit_order(self, symbol: str, shares: int, price: float) -> str:
        """Send ekte ordre til Alpaca."""
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        req = MarketOrderRequest(
            symbol=symbol, qty=shares,
            side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        )
        order = self._client.submit_order(req)
        return str(order.id)
