"""
data/fetcher.py
Henter politikerhandler fra:
- housestockwatcher.com (Representantene)
- senatestockwatcher.com (Senatet)

Begge har gratis API. Ingen API-nøkkel nødvendig.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Optional
import hashlib

logger = logging.getLogger(__name__)


@dataclass
class PoliticianTrade:
    """Én enkelt politikerhandel."""
    trade_id: str                    # Unik hash av handelen
    politician: str                  # Navn
    chamber: str                     # "house" eller "senate"
    party: str                       # "Republican", "Democrat" osv
    state: str                       # Stat de representerer
    symbol: str                      # Aksjesymbol
    asset_name: str                  # Fullt navn på selskapet
    trade_type: str                  # "Purchase", "Sale", "Sale (Full)", "Exchange"
    amount_low: int                  # Nedre grense for handelsbeløp
    amount_high: int                 # Øvre grense
    transaction_date: date           # Dato for handelen
    disclosure_date: date            # Dato innlevert (viktig: forsinkelse = signal!)
    filing_delay_days: int           # Antall dager forsinket
    is_option: bool                  # Opsjon = kraftigere signal
    committee: str = ""              # Komitémedlemskap (Defence, Finance osv)
    notes: str = ""

    @property
    def avg_amount(self) -> int:
        return (self.amount_low + self.amount_high) // 2

    @property
    def is_late(self) -> bool:
        """Mer enn 45 dager forsinkelse = brøt STOCK Act."""
        return self.filing_delay_days > 45

    @property
    def is_suspiciously_late(self) -> bool:
        """Mer enn 90 dager = sannsynligvis skjuler noe."""
        return self.filing_delay_days > 90


AMOUNT_RANGES = {
    "$1,001 - $15,000":    (1001,    15000),
    "$15,001 - $50,000":   (15001,   50000),
    "$50,001 - $100,000":  (50001,  100000),
    "$100,001 - $250,000": (100001, 250000),
    "$250,001 - $500,000": (250001, 500000),
    "$500,001 - $1,000,000": (500001, 1000000),
    "$1,000,001 - $5,000,000": (1000001, 5000000),
    "$5,000,001 - $25,000,000": (5000001, 25000000),
    "Over $25,000,000": (25000001, 50000000),
}


def _parse_amount(amount_str: str) -> tuple[int, int]:
    for key, val in AMOUNT_RANGES.items():
        if key.replace(",", "").replace("$", "").replace(" ", "") in \
           amount_str.replace(",", "").replace("$", "").replace(" ", ""):
            return val
    # Fallback: prøv å parse direkte
    cleaned = amount_str.replace("$", "").replace(",", "").strip()
    try:
        v = int(cleaned)
        return v, v
    except:
        return 1000, 15000


def _make_trade_id(politician: str, symbol: str, date_str: str, trade_type: str) -> str:
    raw = f"{politician}{symbol}{date_str}{trade_type}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _fetch_json(url: str, timeout: int = 15) -> dict | list:
    """Henter JSON fra URL med retry."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "PolitiBot/1.0 (research tool)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"Klarte ikke hente {url}: {e}")
            time.sleep(2 ** attempt)


class PoliticianTradesFetcher:
    """
    Henter og parserer politikerhandler fra begge kamre.
    Cacher resultater for å unngå unødvendige API-kall.
    """

    HOUSE_API  = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
    SENATE_API = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"

    # Komitémedlemskap — manuelt kuratert for de viktigste
    COMMITTEE_MAP = {
        # Forsvar-komiteen → kjøper forsvarsaksjer
        "Armed Services": ["LMT", "RTX", "NOC", "BA", "GD", "HII", "LDOS"],
        # Finans → banker og fintech
        "Financial Services": ["JPM", "BAC", "GS", "MS", "V", "MA"],
        "Banking": ["JPM", "BAC", "WFC", "C"],
        # Helse
        "Energy and Commerce": ["UNH", "CVS", "CI", "ANTM"],
        # Tech
        "Science, Space, and Technology": ["NVDA", "AMD", "INTC", "MSFT", "GOOGL"],
        # Energi
        "Energy": ["XOM", "CVX", "COP", "SLB"],
        # Landbruk
        "Agriculture": ["DE", "ADM", "BG"],
    }

    def __init__(self):
        self._cache: dict[str, list[PoliticianTrade]] = {}
        self._last_fetch: dict[str, float] = {}
        self._cache_ttl = 3600  # 1 time cache

    def fetch_all(self, days_back: int = 365) -> list[PoliticianTrade]:
        """Henter alle handler fra begge kamre de siste N dagene."""
        cutoff = date.today() - timedelta(days=days_back)
        trades = []

        for chamber, url in [("house", self.HOUSE_API), ("senate", self.SENATE_API)]:
            logger.info(f"Henter {chamber}-handler...")
            try:
                raw = self._fetch_cached(chamber, url)
                parsed = self._parse(raw, chamber, cutoff)
                trades.extend(parsed)
                logger.info(f"  → {len(parsed)} handler fra {chamber}")
            except Exception as e:
                logger.error(f"Feil ved henting av {chamber}: {e}")

        logger.info(f"Totalt {len(trades)} handler hentet")
        return trades

    def fetch_recent(self, days: int = 7) -> list[PoliticianTrade]:
        """Henter kun siste N dager — for daglig oppdatering."""
        all_trades = self.fetch_all(days_back=days + 50)
        cutoff = date.today() - timedelta(days=days)
        return [t for t in all_trades if t.disclosure_date >= cutoff]

    def _fetch_cached(self, key: str, url: str) -> list:
        now = time.time()
        if key in self._cache and now - self._last_fetch.get(key, 0) < self._cache_ttl:
            return self._cache[key]
        data = _fetch_json(url)
        self._cache[key] = data if isinstance(data, list) else data.get("data", [])
        self._last_fetch[key] = now
        return self._cache[key]

    def _parse(self, raw: list, chamber: str, cutoff: date) -> list[PoliticianTrade]:
        trades = []
        for item in raw:
            try:
                trade = self._parse_item(item, chamber)
                if trade and trade.transaction_date >= cutoff:
                    trades.append(trade)
            except Exception as e:
                logger.debug(f"Parse-feil: {e} | {item}")
        return trades

    def _parse_item(self, item: dict, chamber: str) -> Optional[PoliticianTrade]:
        # Symbol-rensing
        symbol = str(item.get("ticker", "") or item.get("asset_description", "")).strip().upper()
        if not symbol or len(symbol) > 5 or symbol in ("N/A", "--", ""):
            return None
        # Ignorer non-aksjer (real estate, crypto osv for nå)
        if any(c.isdigit() for c in symbol):
            return None

        # Datoer
        tx_date_str = item.get("transaction_date", "") or item.get("transaction_date_str", "")
        disc_date_str = item.get("disclosure_date", "") or item.get("filed_at_date", "")

        try:
            tx_date = _parse_date(tx_date_str)
            disc_date = _parse_date(disc_date_str)
        except:
            return None

        delay = (disc_date - tx_date).days if disc_date >= tx_date else 0

        # Beløp
        amount_str = str(item.get("amount", "") or item.get("asset_value_range", ""))
        lo, hi = _parse_amount(amount_str)

        # Type handel
        trade_type = str(item.get("type", "") or item.get("transaction_type", "")).strip()
        if not trade_type:
            return None

        # Opsjon?
        asset_type = str(item.get("asset_type", "")).lower()
        is_option = "option" in asset_type or "call" in trade_type.lower() or "put" in trade_type.lower()

        # Politiker-navn
        if chamber == "house":
            name = f"{item.get('representative', '')}".strip()
            party = item.get("party", "")
            state = item.get("state", "")
        else:
            name = f"{item.get('first_name', '')} {item.get('last_name', '')}".strip()
            party = item.get("party", "")
            state = item.get("senator_state", "") or item.get("state", "")

        if not name or name.strip() == "":
            return None

        # Komité
        committee = self._guess_committee(name)

        return PoliticianTrade(
            trade_id=_make_trade_id(name, symbol, tx_date_str, trade_type),
            politician=name,
            chamber=chamber,
            party=party,
            state=state,
            symbol=symbol,
            asset_name=item.get("asset_description", symbol),
            trade_type=trade_type,
            amount_low=lo,
            amount_high=hi,
            transaction_date=tx_date,
            disclosure_date=disc_date,
            filing_delay_days=delay,
            is_option=is_option,
            committee=committee,
            notes=item.get("comment", ""),
        )

    def _guess_committee(self, name: str) -> str:
        """Sjekk om politikeren er kjent for komitémedlemskap."""
        # Utvidet liste — legg til manuelt etter research
        KNOWN = {
            "Nancy Pelosi": "Science, Space, and Technology",
            "Dan Crenshaw": "Armed Services",
            "Michael McCaul": "Foreign Affairs",
            "Josh Gottheimer": "Financial Services",
            "David Rouzer": "Agriculture",
            "Tommy Tuberville": "Armed Services",
            "Pat Toomey": "Banking",
        }
        for key, committee in KNOWN.items():
            if key.lower() in name.lower():
                return committee
        return ""


def _parse_date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except:
            continue
    raise ValueError(f"Kan ikke parse dato: {s}")
