"""
polymarket_bot.py — CLI entry point for the Polymarket arbitrage bot.

Rebuilt per the Polymarket Automated Trading Setup: Complete Infrastructure Guide.

Usage
─────
    # Dry run (paper trading, default — always start here)
    python polymarket_bot.py

    # 3 scan cycles in dry-run (for testing)
    python polymarket_bot.py --scans 3

    # Live trading (requires .env key + funded Polygon wallet)
    python polymarket_bot.py --live

    # Custom capital and strategy parameters
    python polymarket_bot.py --capital 50 --min-spread 3 --max-position-pct 5

    # Debug output
    python polymarket_bot.py --log-level DEBUG

Setup checklist for live trading (Section 1)
─────────────────────────────────────────────
1. Generate a Polygon wallet private key and store it in .env:
     POLYMARKET_PRIVATE_KEY=0x...
2. Fund the wallet with USDC on Polygon mainnet (bridge or buy on DEX).
3. Approve the Polymarket CLOB Exchange contract to spend your USDC
   (py_clob_client does this on first run, or do it manually on app.polymarket.com).
4. Run with --live to start live trading.

VPS recommendations (Section 2)
────────────────────────────────
  VPS Lite  (1–2 markets)  : 4 cores, 8 GB RAM, 70 GB NVMe
  VPS Pro   (3–5 markets)  : 6 cores, 16 GB RAM
  VPS Ultra (multi-market) : 24 cores, 64 GB RAM
  All: DDoS protection, auto backups, 1 Gbps+ network

DISCLAIMER
──────────
Trading prediction markets involves real financial risk.
Arbitrage windows last only seconds — spreads can close before you execute.
Only 0.51% of users earned $1,000+.  Past performance does not guarantee
future results.  Never trade with money you cannot afford to lose entirely.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="polymarket_bot",
        description="Polymarket automated arbitrage bot (CLOB + WebSocket)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
strategies implemented (Section 4):
  sum-to-one     buy YES + NO when total < $1.00 − fees  [primary]
  endgame        95–99%% near-certain outcome positions   [secondary]
  combinatorial  multi-outcome price-sum arbitrage        [secondary]
  cross-platform flag price gaps vs other platforms       [detection only]

examples:
  python polymarket_bot.py                          dry run forever
  python polymarket_bot.py --scans 5                dry run 5 cycles
  python polymarket_bot.py --live --capital 50      live trading $50
  python polymarket_bot.py --log-level DEBUG        verbose output
        """,
    )

    # Mode
    p.add_argument("--live", action="store_true", default=False,
                   help="Enable live trading (default: dry run)")

    # Capital (Section 5)
    p.add_argument("--capital", type=float, default=50.0, metavar="USDC",
                   help="Starting capital in USDC (default: 50)")
    p.add_argument("--max-bet", type=float, default=5.0, metavar="USDC",
                   help="Max single bet in USDC (default: 5)")
    p.add_argument("--max-position-pct", type=float, default=5.0, metavar="PCT",
                   help="Max %% per market position, Section 5 (default: 5)")
    p.add_argument("--max-exposure-pct", type=float, default=10.0, metavar="PCT",
                   help="Max %% total portfolio exposure, Section 5 (default: 10)")

    # Strategy (Section 4)
    p.add_argument("--min-spread", type=float, default=3.0, metavar="PCT",
                   help="Min spread %% for S2O arb (default: 3, guide: need 3%%+ to cover 2%% fee)")
    p.add_argument("--taker-fee", type=float, default=2.0, metavar="PCT",
                   help="Platform taker fee %%, Section 1 (default: 2)")
    p.add_argument("--endgame-min", type=float, default=95.0, metavar="PCT",
                   help="Min probability %% for endgame arb (default: 95)")
    p.add_argument("--endgame-max", type=float, default=99.0, metavar="PCT",
                   help="Max probability %% for endgame arb (default: 99)")

    # Risk (Section 5)
    p.add_argument("--daily-loss-cap", type=float, default=5.0, metavar="PCT",
                   help="Daily loss cap %%, Section 5 (default: 5)")
    p.add_argument("--circuit-breaker", type=int, default=5, metavar="N",
                   help="Circuit breaker: halt after N consecutive losses (default: 5)")

    # Execution (Section 3 / 6)
    p.add_argument("--scan-interval", type=int, default=10, metavar="SEC",
                   help="Seconds between scans (default: 10 — arb requires speed)")
    p.add_argument("--scans", type=int, default=None, metavar="N",
                   help="Stop after N scan cycles (default: run forever)")

    # Logging
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging level (default: INFO)")

    return p.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def confirm_live() -> bool:
    print()
    print("=" * 65)
    print("  WARNING: LIVE TRADING MODE")
    print("=" * 65)
    print("  Real USDC will be placed on Polymarket prediction markets.")
    print()
    print("  Checklist:")
    print("    ✓ POLYMARKET_PRIVATE_KEY set in .env")
    print("    ✓ USDC funded on Polygon mainnet")
    print("    ✓ Tested with --scans 5 in dry-run mode first")
    print("    ✓ VPS with ≥4 cores, 1Gbps+ (Section 2 recommendation)")
    print()
    print("  Reminders:")
    print("    • Arbitrage windows last only seconds — speed is critical")
    print("    • Only 0.51% of users earned $1,000+")
    print("    • 2% platform fee + $0.007 gas per transaction")
    print()
    ans = input("  Type 'yes' to continue: ").strip().lower()
    return ans == "yes"


def build_config(args: argparse.Namespace):
    from trading.polymarket.config import PolymarketConfig
    return PolymarketConfig(
        initial_capital=args.capital,
        max_bet_usd=args.max_bet,
        max_position_pct=args.max_position_pct / 100.0,
        max_portfolio_exposure_pct=args.max_exposure_pct / 100.0,
        s2o_min_spread_pct=args.min_spread / 100.0,
        taker_fee_rate=args.taker_fee / 100.0,
        endgame_min_prob=args.endgame_min / 100.0,
        endgame_max_prob=args.endgame_max / 100.0,
        daily_loss_cap_pct=args.daily_loss_cap / 100.0,
        circuit_breaker_consecutive_losses=args.circuit_breaker,
        scan_interval_s=args.scan_interval,
        dry_run=not args.live,
        log_level=args.log_level,
    )


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    if args.live:
        if not confirm_live():
            logger.info("Aborted.")
            sys.exit(0)

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    if args.live and not private_key:
        logger.error(
            "POLYMARKET_PRIVATE_KEY is not set.\n"
            "Copy .env.example → .env and add your Polygon wallet key."
        )
        sys.exit(1)

    config = build_config(args)
    logger.info(
        f"Polymarket bot starting  |  "
        f"{'LIVE' if args.live else 'DRY RUN'}  |  "
        f"capital=${args.capital:.0f}  min_spread={args.min_spread:.0f}%  "
        f"max_pos={args.max_position_pct:.0f}%  max_exp={args.max_exposure_pct:.0f}%"
    )

    from trading.polymarket.bot import PolymarketBot
    bot = PolymarketBot(config=config, private_key=private_key)
    bot.run(max_scans=args.scans)


if __name__ == "__main__":
    main()
