"""
cli.py — PolitiBot kommandolinje

python cli.py scan                     # Vis topp-signaler akkurat nå
python cli.py scan --days 60           # Siste 60 dager
python cli.py run --paper              # Kjør paper-bot
python cli.py run --live --arm         # Kjør live (krever begge flagg)
python cli.py top --n 20               # Vis topp 20 politikere historisk
python cli.py status                   # Vis åpne posisjoner
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from pathlib import Path


def cmd_scan(args):
    """Scan etter signaler uten å handle."""
    from bot import PolitiBot
    print(f"\n🔍 Scanner politikerhandler siste {args.days} dager...\n")
    bot = PolitiBot({"paper": True, "initial_capital": 100_000})
    signals = bot.run_once(days_back=args.days)

    if not signals:
        print("Ingen signaler funnet.")
        return

    print(f"\n{'='*70}")
    print(f"  TOPP SIGNALER ({len(signals)} totalt)")
    print(f"{'='*70}")

    for i, sig in enumerate(signals[:args.n], 1):
        icon = "🚨" if sig.total_score >= 80 else "📈" if sig.total_score >= 65 else "👀"
        print(f"\n{i:2}. {icon} {sig.trade.symbol:6s} | Score: {sig.total_score:.0f}/100 | {sig.recommendation}")
        print(f"    Politiker:  {sig.trade.politician} ({sig.trade.chamber.upper()}, {sig.trade.party})")
        print(f"    Handel:     {sig.trade.trade_type} | ${sig.trade.avg_amount:,}")
        print(f"    Dato:       {sig.trade.transaction_date} → innlevert {sig.trade.disclosure_date}")
        if sig.trade.filing_delay_days > 45:
            print(f"    ⚠️  {sig.trade.filing_delay_days} DAGER FORSINKET!")
        if sig.trade.is_option:
            print(f"    🎯 OPSJON kjøpt — høy konviksjonsgrad!")
        if sig.trade.committee:
            print(f"    Komité: {sig.trade.committee}")
        print(f"    Grunner:")
        for r in sig.reasons[:4]:
            print(f"      → {r}")
        print(f"    Anbefaling: {sig.urgency} | Størrelse: {sig.suggested_position_size}")

    print(f"\n{'='*70}")
    print("  ⚠️  ADVARSEL: 45-dagers forsinkelse betyr at markedet kan ha")
    print("      allerede reagert på disse nyhetene. Paper trade først!")
    print(f"{'='*70}\n")


def cmd_top(args):
    """Vis topp-politikere basert på historisk profil."""
    from scoring.engine import POLITICIAN_PROFILES
    print(f"\n{'='*60}")
    print("  🏆 TOPP POLITIKERE — Historisk Alpha")
    print(f"{'='*60}")

    ranked = sorted(
        POLITICIAN_PROFILES.items(),
        key=lambda x: x[1].get("historical_alpha", 0),
        reverse=True
    )
    for i, (name, profile) in enumerate(ranked[:args.n], 1):
        alpha = profile.get("historical_alpha", 0)
        bar = "█" * int(alpha * 20)
        print(f"\n{i:2}. {name}")
        print(f"    Alpha: {bar} {alpha:.0%}")
        print(f"    Sektorer: {', '.join(profile.get('sectors', []))}")
        if profile.get("late_filer"):
            print(f"    ⚠️  Kronisk sein innleverer")
        if profile.get("notes"):
            print(f"    📝 {profile['notes']}")
    print()


def cmd_run(args):
    """Kjør boten i paper eller live modus."""
    from bot import PolitiBot

    if args.live:
        if not args.arm:
            print("❌ Live-modus krever BEGGE: --live --arm")
            sys.exit(1)

        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret  = os.environ.get("ALPACA_SECRET_KEY", "")
        if not api_key or not secret:
            print("❌ Sett ALPACA_API_KEY og ALPACA_SECRET_KEY miljøvariabler")
            sys.exit(1)

        print("\n" + "="*60)
        print("  ⚠️  LIVE TRADING — EKTE PENGER!")
        print("  Skriv BEKREFT for å fortsette:")
        if input("  > ").strip() != "BEKREFT":
            print("Avbrutt.")
            sys.exit(0)

        import time
        print("  Starter om 5 sekunder... Ctrl-C for å avbryte")
        time.sleep(5)

        cfg = {
            "paper": False,
            "alpaca_api_key": api_key,
            "alpaca_secret_key": secret,
            "initial_capital": args.capital,
        }
    else:
        print(f"\n📋 Starter PAPER-modus | Kapital: ${args.capital:,}\n")
        cfg = {"paper": True, "initial_capital": args.capital}

    bot = PolitiBot(cfg)
    bot.start()


def cmd_status(args):
    """Vis siste signaler fra logg."""
    log_dir = Path("logs")
    if not log_dir.exists():
        print("Ingen logger funnet. Kjør 'scan' eller 'run' først.")
        return

    signal_files = sorted(log_dir.glob("signals_*.json"), reverse=True)
    if not signal_files:
        print("Ingen signal-filer funnet.")
        return

    with open(signal_files[0]) as f:
        signals = json.load(f)

    print(f"\n📊 Siste signaler ({signal_files[0].name}):")
    print(f"{'='*60}")
    for s in signals[:10]:
        icon = "🚨" if s["score"] >= 80 else "📈"
        print(f"{icon} {s['symbol']:6s} | {s['score']:.0f}/100 | {s['recommendation']:10s} | {s['politician']}")


def main():
    parser = argparse.ArgumentParser(
        description="🇺🇸 PolitiBot — Kopier de smarte pengene",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # scan
    scan_p = sub.add_parser("scan", help="Scan etter signaler")
    scan_p.add_argument("--days", type=int, default=30, help="Antall dager tilbake (default: 30)")
    scan_p.add_argument("--n", type=int, default=15, help="Antall signaler å vise")

    # top
    top_p = sub.add_parser("top", help="Vis topp-politikere")
    top_p.add_argument("--n", type=int, default=10)

    # run
    run_p = sub.add_parser("run", help="Kjør boten")
    run_p.add_argument("--paper", action="store_true", default=True)
    run_p.add_argument("--live", action="store_true")
    run_p.add_argument("--arm", action="store_true")
    run_p.add_argument("--capital", type=int, default=100_000)

    # status
    sub.add_parser("status", help="Vis åpne posisjoner og siste signaler")

    args = parser.parse_args()
    {
        "scan": cmd_scan,
        "top": cmd_top,
        "run": cmd_run,
        "status": cmd_status,
    }[args.command](args)


if __name__ == "__main__":
    main()
