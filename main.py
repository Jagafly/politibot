"""
PolitiBot — Alt i én fil for enkel deploy
Kjører automatisk hvert 60. minutt på Railway
"""

import logging
import time
import json
import hashlib
import urllib.request
import uuid
from datetime import datetime, date, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================
# DATA — Henter politikerhandler
# ============================================================

AMOUNT_RANGES = {
    "1001":    (1001, 15000),
    "15001":   (15001, 50000),
    "50001":   (50001, 100000),
    "100001":  (100001, 250000),
    "250001":  (250001, 500000),
    "500001":  (500001, 1000000),
    "1000001": (1000001, 5000000),
}


def parse_amount(s: str) -> tuple:
    clean = s.replace("$", "").replace(",", "").replace(" ", "")
    for key, val in AMOUNT_RANGES.items():
        if key in clean:
            return val
    return (1000, 15000)


def parse_date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except:
            continue
    raise ValueError(f"Kan ikke parse dato: {s}")


def fetch_json(url: str) -> list:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://housestockwatcher.com/",
        "Origin": "https://housestockwatcher.com",
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == 2:
                logger.error(f"Klarte ikke hente {url}: {e}")
                return []
            time.sleep(2 ** attempt)
    return []


@dataclass
class Trade:
    trade_id: str
    politician: str
    chamber: str
    party: str
    symbol: str
    trade_type: str
    amount_low: int
    amount_high: int
    transaction_date: date
    disclosure_date: date
    filing_delay_days: int
    is_option: bool
    committee: str = ""

    @property
    def avg_amount(self):
        return (self.amount_low + self.amount_high) // 2

    @property
    def is_suspiciously_late(self):
        return self.filing_delay_days > 90

    @property
    def is_late(self):
        return self.filing_delay_days > 45


def fetch_trades(days_back: int = 45) -> list:
    cutoff = date.today() - timedelta(days=days_back)
    trades = []

    sources = [
        ("house",  [
            "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
            "https://housestockwatcher.com/api/transactions",
        ]),
        ("senate", [
            "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
            "https://senatestockwatcher.com/api/transactions",
        ]),
    ]

    for chamber, urls in sources:
        logger.info(f"  Henter {chamber}-data...")
        raw = []
        for url in urls:
            raw = fetch_json(url)
            if raw:
                break
            logger.warning(f"  {url} feilet, prøver neste...")
        if not raw:
            logger.error(f"  Alle {chamber}-URLer feilet!")
            continue

        for item in raw:
            try:
                symbol = str(item.get("ticker", "") or "").strip().upper()
                if not symbol or len(symbol) > 5 or not symbol.isalpha():
                    continue

                tx_str   = item.get("transaction_date", "") or ""
                disc_str = item.get("disclosure_date", "") or item.get("filed_at_date", "")
                if not tx_str or not disc_str:
                    continue

                tx_date   = parse_date(tx_str)
                disc_date = parse_date(disc_str)

                if tx_date < cutoff:
                    continue

                delay = max(0, (disc_date - tx_date).days)
                lo, hi = parse_amount(str(item.get("amount", "") or ""))
                trade_type = str(item.get("type", "") or item.get("transaction_type", "")).strip()
                if not trade_type:
                    continue

                asset_type = str(item.get("asset_type", "")).lower()
                is_option = "option" in asset_type

                if chamber == "house":
                    name  = str(item.get("representative", "")).strip()
                    party = item.get("party", "")
                else:
                    name  = f"{item.get('first_name','')} {item.get('last_name','')}".strip()
                    party = item.get("party", "")

                if not name:
                    continue

                tid = hashlib.md5(f"{name}{symbol}{tx_str}{trade_type}".encode()).hexdigest()[:10]
                committee = COMMITTEE_MAP.get(name, "")

                trades.append(Trade(
                    trade_id=tid, politician=name, chamber=chamber, party=party,
                    symbol=symbol, trade_type=trade_type,
                    amount_low=lo, amount_high=hi,
                    transaction_date=tx_date, disclosure_date=disc_date,
                    filing_delay_days=delay, is_option=is_option,
                    committee=committee,
                ))
            except Exception as e:
                logger.debug(f"Parse-feil: {e}")
                continue

        logger.info(f"  → {len([t for t in trades if t.chamber == chamber])} handler fra {chamber}")

    return trades


# ============================================================
# SCORING — Ranger handler etter mistanksgrad
# ============================================================

POLITICIAN_PROFILES = {
    "Nancy Pelosi":    {"alpha": 0.92, "committee": "Science, Space, and Technology"},
    "Dan Crenshaw":    {"alpha": 0.71, "committee": "Armed Services"},
    "Tommy Tuberville":{"alpha": 0.68, "committee": "Armed Services"},
    "Josh Gottheimer": {"alpha": 0.65, "committee": "Financial Services"},
    "Michael McCaul":  {"alpha": 0.60, "committee": "Foreign Affairs"},
}

COMMITTEE_MAP = {name: p["committee"] for name, p in POLITICIAN_PROFILES.items()}

COMMITTEE_STOCKS = {
    "Armed Services":             ["LMT","RTX","NOC","BA","GD","HII"],
    "Financial Services":         ["JPM","BAC","GS","MS","V","MA"],
    "Science, Space, and Technology": ["NVDA","AMD","MSFT","GOOGL","META","INTC"],
    "Energy":                     ["XOM","CVX","COP","SLB"],
}


@dataclass
class Signal:
    trade: Trade
    score: float
    recommendation: str
    urgency: str
    size: str
    reasons: list


def score_trades(trades: list) -> list:
    purchases = [t for t in trades if "purchase" in t.trade_type.lower()]
    if not purchases:
        return []

    # Klyngedeteksjon
    clusters = defaultdict(list)
    for t in purchases:
        clusters[t.symbol].append(t.politician)

    signals = []
    seen = set()

    for trade in purchases:
        key = f"{trade.symbol}_{trade.transaction_date}"
        if key in seen:
            continue
        seen.add(key)

        score = 0.0
        reasons = []

        # 1. Kjent politiker
        if trade.politician in POLITICIAN_PROFILES:
            alpha = POLITICIAN_PROFILES[trade.politician]["alpha"]
            pts = alpha * 35
            score += pts
            reasons.append(f"Kjent politiker {trade.politician} (historisk alpha {alpha:.0%})")

        # 2. Beløp
        if trade.avg_amount >= 1_000_000:
            score += 15; reasons.append(f"Mega-handel: ${trade.avg_amount:,}")
        elif trade.avg_amount >= 250_000:
            score += 10; reasons.append(f"Stor handel: ${trade.avg_amount:,}")
        elif trade.avg_amount >= 50_000:
            score += 5;  reasons.append(f"Medium handel: ${trade.avg_amount:,}")
        else:
            score += 2;  reasons.append(f"Liten handel: ${trade.avg_amount:,}")

        # 3. Opsjon
        if trade.is_option:
            score += 8
            reasons.append("Opsjon kjøpt — høy konviksjonsgrad!")

        # 4. Forsinkelse
        if trade.is_suspiciously_late:
            score += 7
            reasons.append(f"⚠️ {trade.filing_delay_days} dager forsinket!")
        elif trade.is_late:
            score += 4
            reasons.append(f"Sein innlevering: {trade.filing_delay_days} dager")

        # 5. Komité-match
        committee_stocks = COMMITTEE_STOCKS.get(trade.committee, [])
        if trade.symbol in committee_stocks:
            score += 10
            reasons.append(f"Handel i egen komité ({trade.committee})!")

        # 6. Klynge
        unique = set(clusters[trade.symbol])
        if len(unique) >= 3:
            score += 20
            reasons.append(f"🚨 KLYNGE: {len(unique)} politikere kjøpte {trade.symbol}!")
        elif len(unique) == 2:
            score += 10
            reasons.append(f"2 politikere kjøpte {trade.symbol}")

        score = min(score, 100)

        if score >= 80:
            rec, urgency, size = "STRONG BUY", "IMMEDIATE", "FULL"
        elif score >= 65:
            rec, urgency, size = "BUY", "TODAY", "HALF"
        elif score >= 40:
            rec, urgency, size = "WATCH", "THIS_WEEK", "QUARTER"
        else:
            continue

        signals.append(Signal(trade=trade, score=score, recommendation=rec,
                              urgency=urgency, size=size, reasons=reasons))

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals


# ============================================================
# TRADING — Paper-handler
# ============================================================

@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    stop_loss: float
    take_profit: float
    politician: str
    score: float
    pnl: float = 0.0


class PaperTrader:
    def __init__(self, capital: float = 100_000):
        self.capital = capital
        self.cash = capital
        self.positions: dict[str, Position] = {}
        self.closed: list[Position] = []
        self.MAX_POSITIONS = 5

    def try_buy(self, signal: Signal) -> bool:
        sym = signal.trade.symbol
        if sym in self.positions or len(self.positions) >= self.MAX_POSITIONS:
            return False

        price = self._get_price(sym)
        if not price:
            return False

        stop  = round(price * 0.92, 2)   # 8% stop
        tp    = round(price * 1.20, 2)   # 20% take profit
        risk  = self.cash * 0.02 * {"FULL": 1.0, "HALF": 0.5, "QUARTER": 0.25}.get(signal.size, 0.5)
        shares = max(1, int(risk / (price - stop)))
        shares = min(shares, int(self.cash * 0.10 / price))

        if shares <= 0 or shares * price > self.cash:
            return False

        self.cash -= shares * price
        self.positions[sym] = Position(
            symbol=sym, shares=shares, entry_price=price,
            stop_loss=stop, take_profit=tp,
            politician=signal.trade.politician, score=signal.score,
        )
        logger.info(f"✅ PAPER BUY:  {shares}x {sym} @ ${price:.2f} | SL=${stop:.2f} TP=${tp:.2f} | {signal.trade.politician}")
        return True

    def update(self) -> list[str]:
        closed = []
        for sym, pos in list(self.positions.items()):
            price = self._get_price(sym)
            if not price:
                continue

            # Trailing stop (12%)
            new_sl = round(price * 0.88, 2)
            if new_sl > pos.stop_loss:
                pos.stop_loss = new_sl

            if price <= pos.stop_loss or price >= pos.take_profit:
                reason = "stop-loss" if price <= pos.stop_loss else "take-profit"
                pnl = (price - pos.entry_price) * pos.shares
                pos.pnl = round(pnl, 2)
                self.cash += price * pos.shares
                self.closed.append(pos)
                del self.positions[sym]
                icon = "🟢" if pnl >= 0 else "🔴"
                logger.info(f"{icon} LUKKET {sym} [{reason}]: PnL=${pnl:+,.2f} ({(price/pos.entry_price-1)*100:+.1f}%)")
                closed.append(sym)
        return closed

    def summary(self) -> dict:
        prices = {sym: self._get_price(sym) or pos.entry_price
                  for sym, pos in self.positions.items()}
        open_val = sum(pos.shares * prices.get(sym, pos.entry_price)
                       for sym, pos in self.positions.items())
        open_pnl = sum((prices.get(sym, pos.entry_price) - pos.entry_price) * pos.shares
                       for sym, pos in self.positions.items())
        closed_pnl = sum(p.pnl for p in self.closed)
        wins = [p for p in self.closed if p.pnl > 0]

        return {
            "equity":      round(self.cash + open_val, 2),
            "cash":        round(self.cash, 2),
            "open_pnl":    round(open_pnl, 2),
            "closed_pnl":  round(closed_pnl, 2),
            "positions":   len(self.positions),
            "win_rate":    f"{len(wins)/max(len(self.closed),1)*100:.0f}%",
            "total_trades": len(self.closed),
        }

    def _get_price(self, symbol: str) -> Optional[float]:
        try:
            import yfinance as yf
            hist = yf.Ticker(symbol).history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            logger.debug(f"Pris-feil {symbol}: {e}")
        return None


# ============================================================
# HOVED-LOOP
# ============================================================

def main():
    logger.info("=" * 60)
    logger.info("🇺🇸 POLITIBOT — Kopier Kongressens smarte penger")
    logger.info(f"   Starttid: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    logger.info(f"   Startkapital: $100,000 (paper-penger)")
    logger.info("=" * 60)

    trader = PaperTrader(capital=100_000)
    run = 0

    while True:
        run += 1
        logger.info(f"\n{'─'*60}")
        logger.info(f"🔄 KJØRING #{run} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        logger.info(f"{'─'*60}")

        try:
            # Hent handler
            logger.info("📡 Henter politikerhandler fra Kongressen...")
            trades = fetch_trades(days_back=45)
            logger.info(f"   Totalt {len(trades)} handler hentet")

            # Score
            signals = score_trades(trades)
            logger.info(f"📊 {len(signals)} signaler generert")

            if signals:
                logger.info("\n🏆 TOPP 5 SIGNALER:")
                for i, sig in enumerate(signals[:5], 1):
                    icon = "🚨" if sig.score >= 80 else "📈" if sig.score >= 65 else "👀"
                    logger.info(f"  {i}. {icon} {sig.trade.symbol:6s} | Score {sig.score:.0f}/100 | {sig.recommendation:10s} | {sig.trade.politician}")
                    for r in sig.reasons[:2]:
                        logger.info(f"       → {r}")

                # Handle topp signaler
                logger.info("\n💰 Vurderer nye kjøp...")
                bought = 0
                for sig in signals:
                    if bought >= 2:
                        break
                    if sig.recommendation in ("STRONG BUY", "BUY"):
                        if trader.try_buy(sig):
                            bought += 1

                if bought == 0:
                    logger.info("   Ingen nye kjøp — enten har vi maks posisjoner eller ingen gode signaler")

            # Oppdater åpne posisjoner
            if trader.positions:
                logger.info("\n📈 Oppdaterer åpne posisjoner...")
                trader.update()

            # Portefølje-status
            s = trader.summary()
            logger.info(f"\n💼 PORTEFØLJE STATUS:")
            logger.info(f"   💵 Egenkapital:  ${s['equity']:>12,.2f}")
            logger.info(f"   📈 Åpen PnL:     ${s['open_pnl']:>+12,.2f}")
            logger.info(f"   ✅ Lukket PnL:   ${s['closed_pnl']:>+12,.2f}")
            logger.info(f"   📂 Posisjoner:   {s['positions']}")
            logger.info(f"   🎯 Win rate:     {s['win_rate']}")
            logger.info(f"   📊 Handler totalt: {s['total_trades']}")

            if trader.positions:
                logger.info("\n   Åpne posisjoner:")
                for sym, pos in trader.positions.items():
                    price = trader._get_price(sym) or pos.entry_price
                    pnl = (price - pos.entry_price) * pos.shares
                    icon = "🟢" if pnl >= 0 else "🔴"
                    logger.info(f"   {icon} {sym:6s}: {pos.shares} aksjer | Kjøpt @ ${pos.entry_price:.2f} | Nå ${price:.2f} | PnL ${pnl:+,.0f} | {pos.politician}")

        except Exception as e:
            logger.error(f"❌ Feil denne kjøringen: {e}", exc_info=True)
            logger.info("   Prøver igjen om 60 min...")

        logger.info(f"\n😴 Sover 60 minutter til neste sjekk...")
        time.sleep(3600)


if __name__ == "__main__":
    main()
