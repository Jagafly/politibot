"""
scoring/engine.py
Scoringsmotor — det som faktisk gjør denne boten spesiell.

Rangerer:
1. Politikere etter historisk "treffsikkerhet" (tracker records)
2. Enkelthandler etter mistanksgrad og timing
3. Klyngedeteksjon — når FLERE politikere kjøper samme aksje

Jo høyere score, jo raskere handler boten.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from data.fetcher import PoliticianTrade

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────
# Politiker-profiler (manuelt kuratert + AI-oppdatert)
# Basert på offentlig analyse av historiske handler
# ────────────────────────────────────────────────
POLITICIAN_PROFILES: dict[str, dict] = {
    "Nancy Pelosi": {
        "historical_alpha": 0.92,   # Slår markedet 92% av tiden (historisk)
        "sectors": ["tech", "pharma"],
        "trust_score": 95,
        "late_filer": False,
        "notes": "Ektefelles handler — ofte i NVIDIA, Apple, Tesla",
    },
    "Dan Crenshaw": {
        "historical_alpha": 0.71,
        "sectors": ["defense", "energy"],
        "trust_score": 72,
        "late_filer": True,
    },
    "Tommy Tuberville": {
        "historical_alpha": 0.68,
        "sectors": ["defense"],
        "trust_score": 70,
        "late_filer": True,
        "notes": "Kjøpte forsvarsaksjer mens han satt i Armed Services",
    },
    "Josh Gottheimer": {
        "historical_alpha": 0.65,
        "sectors": ["fintech", "banking"],
        "trust_score": 65,
        "late_filer": False,
    },
    "Michael McCaul": {
        "historical_alpha": 0.60,
        "sectors": ["tech", "defense"],
        "trust_score": 62,
        "late_filer": True,
    },
}

# Komité → bonus for relevante aksjer
COMMITTEE_SECTOR_MAP = {
    "Armed Services": ["LMT", "RTX", "NOC", "BA", "GD", "HII", "LDOS", "CACI", "SAIC"],
    "Financial Services": ["JPM", "BAC", "GS", "MS", "V", "MA", "SQ", "PYPL"],
    "Banking": ["JPM", "BAC", "WFC", "C", "USB", "PNC"],
    "Energy and Commerce": ["UNH", "CVS", "CI", "HUM", "CNC"],
    "Science, Space, and Technology": ["NVDA", "AMD", "INTC", "MSFT", "GOOGL", "META"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "HAL", "EOG"],
    "Agriculture": ["DE", "ADM", "BG", "MOS", "NTR"],
}


@dataclass
class TradeSignal:
    """Et signal som boten bør handle på."""
    trade: PoliticianTrade
    total_score: float          # 0–100
    politician_score: float
    trade_score: float
    cluster_score: float
    recommendation: str         # "STRONG BUY", "BUY", "WATCH", "SKIP"
    reasons: list[str]
    urgency: str               # "IMMEDIATE", "TODAY", "THIS_WEEK"
    suggested_position_size: str  # "FULL", "HALF", "QUARTER"


class PoliticianScorer:
    """Scorer enkeltpolitikere basert på historikk og profil."""

    def score(self, politician: str, trades_history: list[PoliticianTrade]) -> float:
        """Returnerer 0–40 poeng for politikeren."""
        pts = 0.0
        reasons = []

        # Kjent høy-performer
        if politician in POLITICIAN_PROFILES:
            profile = POLITICIAN_PROFILES[politician]
            alpha = profile.get("historical_alpha", 0.5)
            pts += alpha * 35   # Maks 35 poeng for kjent track record
            reasons.append(f"Kjent profil: alpha={alpha:.0%}")

        # Sen innleverer = skjuler noe
        late_count = sum(1 for t in trades_history if t.is_late and t.politician == politician)
        if late_count > 5:
            pts += 5
            reasons.append(f"Kronisk sein: {late_count} sene innleveringer")

        return min(pts, 40.0)


class TradeScorer:
    """Scorer enkelthandler basert på en rekke faktorer."""

    def score(self, trade: PoliticianTrade) -> tuple[float, list[str]]:
        """Returnerer (score 0–40, reasons)."""
        pts = 0.0
        reasons = []

        # 1. Kun kjøp er interessant (salg kan være for utgifter)
        if "purchase" not in trade.trade_type.lower():
            return 0.0, ["Salg — ignorer for kjøpssignal"]

        # 2. Størrelse på handel
        if trade.avg_amount >= 1_000_000:
            pts += 15
            reasons.append(f"Mega-handel: ${trade.avg_amount:,}")
        elif trade.avg_amount >= 250_000:
            pts += 10
            reasons.append(f"Stor handel: ${trade.avg_amount:,}")
        elif trade.avg_amount >= 50_000:
            pts += 5
            reasons.append(f"Medium handel: ${trade.avg_amount:,}")
        else:
            pts += 2
            reasons.append(f"Liten handel: ${trade.avg_amount:,}")

        # 3. Opsjon = de tror VIRKELIG på det (høy konviksjonsgrad)
        if trade.is_option:
            pts += 8
            reasons.append("Opsjon kjøpt — høy konviksjonsgrad!")

        # 4. Forsinkelse-signal
        if trade.is_suspiciously_late:
            pts += 7
            reasons.append(f"⚠️ {trade.filing_delay_days} dager forsinket — prøver å skjule")
        elif trade.is_late:
            pts += 4
            reasons.append(f"Sein: {trade.filing_delay_days} dager")

        # 5. Komité-match (handler i sin egen sektor = insider-info)
        if trade.committee and trade.symbol in COMMITTEE_SECTOR_MAP.get(trade.committee, []):
            pts += 10
            reasons.append(f"🎯 Handel i egen komité ({trade.committee}) — sterk insider-signal!")

        return min(pts, 40.0), reasons


class ClusterDetector:
    """
    Klyngedeteksjon — det kraftigste signalet.
    Når 3+ politikere kjøper samme aksje innen 30 dager → sterk korrelasjon med innsidekunnskap.
    """

    def detect_clusters(
        self,
        trades: list[PoliticianTrade],
        window_days: int = 30,
    ) -> dict[str, dict]:
        """
        Returnerer dict: symbol → cluster-info
        Filtrerer kun kjøp.
        """
        purchases = [t for t in trades if "purchase" in t.trade_type.lower()]
        clusters: dict[str, list[PoliticianTrade]] = defaultdict(list)

        cutoff = date.today() - timedelta(days=window_days)
        recent = [t for t in purchases if t.transaction_date >= cutoff]

        for trade in recent:
            clusters[trade.symbol].append(trade)

        result = {}
        for symbol, symbol_trades in clusters.items():
            unique_politicians = set(t.politician for t in symbol_trades)
            if len(unique_politicians) >= 2:
                total_amount = sum(t.avg_amount for t in symbol_trades)
                result[symbol] = {
                    "count": len(unique_politicians),
                    "politicians": list(unique_politicians),
                    "total_amount": total_amount,
                    "trades": symbol_trades,
                    "score": min(len(unique_politicians) * 8, 20),  # Maks 20 poeng
                }

        return result

    def score_for_symbol(self, symbol: str, clusters: dict) -> tuple[float, list[str]]:
        if symbol not in clusters:
            return 0.0, []
        c = clusters[symbol]
        reasons = [
            f"🚨 KLYNGE: {c['count']} politikere kjøpte {symbol} nylig!",
            f"   Politikere: {', '.join(c['politicians'][:5])}",
            f"   Totalt investert: ${c['total_amount']:,}",
        ]
        return float(c["score"]), reasons


class SignalEngine:
    """
    Kombinerer alle scorere til endelige signaler.
    Rangerer etter total score og filtrerer svake signaler.
    """

    MIN_SCORE_FOR_SIGNAL = 40   # Under dette → ignorer

    def __init__(self):
        self.pol_scorer = PoliticianScorer()
        self.trade_scorer = TradeScorer()
        self.cluster_detector = ClusterDetector()

    def generate_signals(
        self,
        trades: list[PoliticianTrade],
        all_history: list[PoliticianTrade],
    ) -> list[TradeSignal]:
        """Generer rangerte signaler fra liste med handler."""

        # Bygg klynger
        clusters = self.cluster_detector.detect_clusters(all_history)

        signals = []
        seen_symbols = set()

        for trade in trades:
            if "purchase" not in trade.trade_type.lower():
                continue

            # Unngå duplikater per symbol per dag
            dedup_key = f"{trade.symbol}_{trade.transaction_date}"
            if dedup_key in seen_symbols:
                continue
            seen_symbols.add(dedup_key)

            # Score de tre dimensjonene
            pol_score = self.pol_scorer.score(trade.politician, all_history)
            trade_pts, trade_reasons = self.trade_scorer.score(trade)
            cluster_pts, cluster_reasons = self.cluster_detector.score_for_symbol(
                trade.symbol, clusters
            )

            total = pol_score + trade_pts + cluster_pts
            all_reasons = trade_reasons + cluster_reasons

            if pol_score > 0:
                all_reasons.insert(0, f"Kjent politiker: {trade.politician} (score={pol_score:.0f})")

            if total < self.MIN_SCORE_FOR_SIGNAL:
                continue

            # Anbefaling
            if total >= 80:
                recommendation = "STRONG BUY"
                urgency = "IMMEDIATE"
                size = "FULL"
            elif total >= 65:
                recommendation = "BUY"
                urgency = "TODAY"
                size = "HALF"
            elif total >= 40:
                recommendation = "WATCH"
                urgency = "THIS_WEEK"
                size = "QUARTER"
            else:
                continue

            signals.append(TradeSignal(
                trade=trade,
                total_score=round(total, 1),
                politician_score=round(pol_score, 1),
                trade_score=round(trade_pts, 1),
                cluster_score=round(cluster_pts, 1),
                recommendation=recommendation,
                reasons=all_reasons,
                urgency=urgency,
                suggested_position_size=size,
            ))

        # Sorter etter score — beste øverst
        signals.sort(key=lambda s: s.total_score, reverse=True)
        logger.info(f"Genererte {len(signals)} signaler fra {len(trades)} handler")
        return signals
