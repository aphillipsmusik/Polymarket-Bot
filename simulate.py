"""
simulate.py — Paper trading simulation CLI.

Runs the full Polymarket bot stack (StrategyEngine, RiskManager, MetricsTracker)
against a fully synthetic market engine — zero network calls, zero real money.

Usage
─────
    # Basic paper trading simulation (15 markets, 1-second ticks, 60 scans)
    python simulate.py

    # Speed up the tick rate and run more markets
    python simulate.py --tick 0.25 --markets 20 --scans 120

    # Bigger capital, tighter spread requirement
    python simulate.py --capital 500 --min-spread 2

    # Debug mode — see every strategy decision
    python simulate.py --log-level DEBUG

    # Fixed random seed for reproducible runs
    python simulate.py --seed 99 --scans 30

Simulation parameters
─────────────────────
  --tick SECS     WebSocket tick interval (default 1.0 s — 1 book update/sec)
  --markets N     Number of synthetic markets (max 20, default 15)
  --seed N        RNG seed for reproducible results (default 42)
  --scans N       Stop after N scan cycles (default: run until Ctrl+C)
  --capital N     Starting USDC (default 50)
  --min-spread N  Minimum spread % for S2O arb (default 3)

Section 6 targets (displayed in dashboard)
──────────────────────────────────────────
  Execution success rate : 99.2%
  Response time          : ~80ms
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Dict, List, Optional

from trading.polymarket.config import PolymarketConfig
from trading.polymarket.data_collector import DataCollector
from trading.polymarket.metrics import MetricsTracker
from trading.polymarket.models import (
    ArbExecution,
    ArbOpportunity,
    ArbResult,
    ArbType,
    ClosedPosition,
    Market,
    OpenPosition,
    OrderBookSnapshot,
    PortfolioState,
)
from trading.polymarket.risk_manager import RiskManager
from trading.polymarket.simulation import SimulatedDataCollector, SimulatedOrderManager
from trading.polymarket.strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)


# ── SimulationBot ─────────────────────────────────────────────────────────────

class SimulationBot:
    """
    Paper-trading bot that wires StrategyEngine + RiskManager + MetricsTracker
    to SimulatedDataCollector + SimulatedOrderManager.

    Identical control flow to PolymarketBot, but all I/O is synthetic.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        live_data: bool = False,
        tick_interval_s: float = 1.0,
        num_markets: int = 15,
        seed: int = 42,
    ) -> None:
        self.config    = config
        self.live_data = live_data

        # Data source: real Polymarket API or fully synthetic simulation
        if live_data:
            self.data = DataCollector(
                config=config,
                on_book_update=self._on_book_update,
            )
        else:
            self.data = SimulatedDataCollector(
                config=config,
                on_book_update=self._on_book_update,
                tick_interval_s=tick_interval_s,
                num_markets=num_markets,
                seed=seed,
            )
        self.orders  = SimulatedOrderManager(config=config)  # always paper-trade
        self.strategy = StrategyEngine(config=config)
        self.risk     = RiskManager(config=config)
        self.metrics  = MetricsTracker(config=config)

        # Portfolio
        self.portfolio = PortfolioState(cash_usdc=config.initial_capital)

        # Internal
        self._markets: Dict[str, Market] = {}
        self._tid_to_cid: Dict[str, str] = {}
        self._book_ts: Dict[str, float] = {}
        self._scan_count = 0
        self._stop = False

        # Live display counters (updated in hot path)
        self._arb_events: List[str] = []        # last 10 arb outcomes for display
        self._last_scan_results: List[dict] = []  # per-market spread data from last scan

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self, max_scans: Optional[int] = None) -> None:
        try:
            asyncio.run(self._run_async(max_scans=max_scans))
        except KeyboardInterrupt:
            logger.info("Simulation stopped by user (Ctrl+C)")

    async def _run_async(self, max_scans: Optional[int] = None) -> None:
        self._print_banner()

        ws_task      = asyncio.create_task(self.data.ws.connect(),   name="ws_feed")
        scan_task    = asyncio.create_task(self._scan_loop(max_scans), name="scanner")
        monitor_task = asyncio.create_task(self._monitor_loop(),      name="monitor")

        try:
            # Wait only for scan_task: when max_scans is reached it returns normally;
            # on Ctrl+C it raises CancelledError propagated from the event loop.
            await asyncio.gather(scan_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Scanner error: {exc}")
        finally:
            # Signal all tasks to stop, then cancel them for fast exit.
            self._stop = True
            self.data.ws.stop()
            for task in (ws_task, monitor_task, scan_task):
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(ws_task, monitor_task, scan_task,
                                   return_exceptions=True),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                pass
            self._print_final_report()

    # ── Task 1: Market scanner ────────────────────────────────────────────────

    async def _scan_loop(self, max_scans: Optional[int] = None) -> None:
        while not self._stop:
            self._scan_count += 1
            t0 = time.monotonic()

            from datetime import datetime
            logger.info(
                f"\n{'─'*65}"
                f"\n  [SIM] Scan #{self._scan_count}  —  "
                f"{datetime.utcnow():%Y-%m-%d %H:%M:%S UTC}"
                f"\n  Risk: {self.risk.status()}"
                f"\n{'─'*65}"
            )

            # Fetch markets (real REST call or synthetic — run in executor to
            # avoid blocking the asyncio event loop with requests.get)
            loop = asyncio.get_event_loop()
            all_markets = await loop.run_in_executor(
                None, lambda: self.data.get_active_markets(limit=200)
            )
            tradeable   = [m for m in all_markets if self._market_passes(m)]
            logger.info(
                f"Markets: {len(all_markets)} synthetic → {len(tradeable)} tradeable"
            )

            # Register new markets
            new_tids: List[str] = []
            for m in tradeable:
                if m.condition_id not in self._markets:
                    self._markets[m.condition_id] = m
                    self._tid_to_cid[m.yes_token.token_id] = m.condition_id
                    self._tid_to_cid[m.no_token.token_id]  = m.condition_id
                    new_tids += [m.yes_token.token_id, m.no_token.token_id]

            if new_tids:
                await self.data.ws.add_subscriptions(new_tids)
                logger.info(f"WS: tracking {len(new_tids)} new synthetic tokens")

            # Batch scan (catches opportunities before WS warms up)
            if not self.risk.state.trading_halted:
                opps = self.strategy.batch_scan(tradeable, self.data.get_book)
                for opp in opps:
                    await self._try_execute(opp)
                    if self.risk.state.trading_halted:
                        break

            # Equity snapshot for Sharpe
            self.metrics.record_equity(self.portfolio.equity)

            self._print_status()
            self.metrics.print_dashboard(
                self.portfolio.equity, self.config.initial_capital
            )

            # Collect per-market spread snapshot for dashboard display.
            # get_book() is an O(1) in-memory lookup — no extra I/O.
            scan_details = []
            for m in tradeable[:30]:
                yb = self.data.get_book(m.yes_token.token_id)
                nb = self.data.get_book(m.no_token.token_id)
                ya = yb.best_ask if yb else None
                na = nb.best_ask if nb else None
                if ya and na:
                    spread_pct = round((1.0 - ya - na) * 100, 3)
                    scan_details.append({
                        "q": m.question[:65],
                        "yes": round(ya, 4),
                        "no": round(na, 4),
                        "sum": round(ya + na, 4),
                        "spread_pct": spread_pct,
                        "arb": spread_pct >= self.config.s2o_min_spread_pct * 100,
                    })
            # Sort: arb opportunities first, then by spread descending
            scan_details.sort(key=lambda x: (x["arb"], x["spread_pct"]), reverse=True)
            self._last_scan_results = scan_details
            await self._on_scan_complete(scan_details)

            scan_ms = (time.monotonic() - t0) * 1000
            logger.debug(f"Scan took {scan_ms:.0f}ms")

            if max_scans and self._scan_count >= max_scans:
                logger.info(f"Reached max_scans={max_scans} — stopping simulation.")
                return

            logger.info(
                f"Next scan in {self.config.scan_interval_s}s  "
                f"| Avg exec latency: {self.orders.avg_execution_ms:.0f}ms"
            )
            await asyncio.sleep(self.config.scan_interval_s)

    # ── Task 2: Position monitor ──────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        import random as _random
        from collections import defaultdict
        from datetime import datetime as _dt
        from trading.polymarket.models import ClosedPosition
        _rng = _random.Random()

        while not self._stop:
            await asyncio.sleep(self.config.position_check_interval_s)
            resolved_any = False

            # ── Stop-loss checks (single leg) ─────────────────────────────
            for pos in list(self.portfolio.open_positions):
                book = self.data.get_book(pos.token_id)
                if book is None or book.best_bid is None:
                    continue
                current_price = book.best_bid
                pnl_pct = (
                    pos.unrealized_pnl(current_price) / pos.cost_basis
                    if pos.cost_basis else 0
                )
                if pos.is_unhedged and pnl_pct < -0.10:
                    logger.warning(f"[SIM] Cutting unhedged position pnl={pnl_pct*100:.1f}%")
                    await self._close_position(pos, current_price, "unhedged_stop")
                    resolved_any = True
                    continue
                if pos.arb_type == ArbType.ENDGAME and pnl_pct < -0.05:
                    logger.warning(f"[SIM] Endgame reversed {pnl_pct*100:.1f}% — closing")
                    await self._close_position(pos, current_price, "endgame_stop")
                    resolved_any = True

            # ── Simulate market resolution for S2O pairs ──────────────────
            # Group hedged S2O positions by condition_id so we always
            # resolve BOTH legs (YES + NO) together — exactly as a real
            # market resolution would. Resolving one leg at a time leaves
            # orphaned positions that block new trades on that market.
            paired: dict = defaultdict(list)
            for pos in self.portfolio.open_positions:
                if pos.arb_type == ArbType.SUM_TO_ONE and not pos.is_unhedged:
                    paired[pos.condition_id].append(pos)

            for cid, legs in paired.items():
                if len(legs) < 2:
                    continue  # incomplete pair — skip
                if _rng.random() > 0.10:
                    continue  # 10% chance per 30s check → ~5-min expected lifetime

                # Randomly pick which outcome wins (YES or NO)
                yes_wins = _rng.random() < 0.5
                now = _dt.utcnow()

                for pos in legs:
                    this_wins = (pos.direction == "YES" and yes_wins) or \
                                (pos.direction == "NO" and not yes_wins)
                    payout = pos.quantity * 1.0 if this_wins else 0.0

                    closed = ClosedPosition(
                        condition_id=pos.condition_id,
                        question=pos.question,
                        token_id=pos.token_id,
                        direction=pos.direction,
                        quantity=pos.quantity,
                        entry_price=pos.entry_price,
                        exit_price=1.0 if this_wins else 0.0,
                        cost_basis=pos.cost_basis,
                        exit_proceeds=round(payout, 4),
                        pnl=round(payout - pos.cost_basis, 4),
                        pnl_pct=round(
                            (payout - pos.cost_basis) / pos.cost_basis, 6
                        ) if pos.cost_basis else 0,
                        entry_time=pos.entry_time,
                        exit_time=now,
                        exit_reason="market_resolved",
                        arb_type=pos.arb_type,
                    )
                    if pos in self.portfolio.open_positions:
                        self.portfolio.open_positions.remove(pos)
                    self.portfolio.closed_positions.append(closed)
                    self.portfolio.cash_usdc += payout

                winner = "YES" if yes_wins else "NO"
                logger.info(
                    f"  [SIM] Market resolved → {winner} wins  "
                    f"{legs[0].question[:45]}"
                )
                resolved_any = True

            # Push dashboard update whenever positions change
            if resolved_any:
                await self._on_resolution()

    async def _on_resolution(self) -> None:
        """Called after market resolution — override in DashboardBot to push state."""
        pass

    async def _on_scan_complete(self, scan_details: List[dict]) -> None:
        """Called at end of each scan with per-market spread data — override in DashboardBot."""
        pass

    # ── WebSocket callback (hot path) ─────────────────────────────────────────

    async def _on_book_update(
        self,
        token_id: str,
        book: OrderBookSnapshot,
    ) -> None:
        if self._stop or self.risk.state.trading_halted:
            return

        now = time.monotonic()
        if now - self._book_ts.get(token_id, 0) < self.config.scan_interval_s / 2:
            return

        cid = self._tid_to_cid.get(token_id)
        if not cid:
            return

        market = self._markets.get(cid)
        if not market or self.portfolio.has_position(cid):
            return

        yes_book = self.data.get_book(market.yes_token.token_id)
        no_book  = self.data.get_book(market.no_token.token_id)

        opp = self.strategy.analyze_market(market, yes_book, no_book)
        if opp is None:
            return

        self._book_ts[token_id] = now
        await self._try_execute(opp)

    # ── Trade execution ───────────────────────────────────────────────────────

    async def _try_execute(self, opp: ArbOpportunity) -> None:
        if not self.risk.approve(opp, self.portfolio):
            return

        logger.info(f"\n  [SIM] OPPORTUNITY: {opp}")

        t0 = time.monotonic()
        execution: ArbExecution = await self.orders.execute_arb(opp)
        elapsed_ms = (time.monotonic() - t0) * 1000

        self.risk.record_execution(execution, self.portfolio)
        self.metrics.record_arb_execution(execution)

        result_str = execution.result.value.upper()
        profit_str = (
            f"  profit=${execution.realized_profit_usd:+.4f}"
            if execution.result == ArbResult.SUCCESS else ""
        )
        event = (
            f"  [{result_str}] {opp.arb_type.value}  "
            f"spread={opp.gross_profit_pct*100:.2f}%  "
            f"exec={elapsed_ms:.0f}ms{profit_str}"
        )
        logger.info(event)
        self._arb_events = ([event] + self._arb_events)[:10]

        if execution.result == ArbResult.SUCCESS:
            self._record_arb_success(execution)
        elif execution.result in (ArbResult.PARTIAL_UNWIND, ArbResult.FAIL_UNWIND):
            self._record_arb_failure(execution)

    def _record_arb_success(self, execution: ArbExecution) -> None:
        from datetime import datetime
        opp = execution.opportunity

        # For sum-to-one arb the profit is locked in the moment both legs
        # fill — regardless of which outcome wins, we collect exactly
        # (1 - ask_yes - ask_no) per unit. Credit it to cash now so the
        # equity curve immediately reflects the gain.
        if opp.arb_type == ArbType.SUM_TO_ONE:
            self.portfolio.cash_usdc += execution.realized_profit_usd

        for leg in execution.filled_legs:
            pos = OpenPosition(
                condition_id=opp.condition_id,
                question=opp.question,
                token_id=leg.token_id,
                direction=leg.outcome_label,
                quantity=leg.size_tokens,
                entry_price=leg.price,
                cost_basis=leg.size_usdc,
                entry_time=datetime.utcnow(),
                arb_type=opp.arb_type,
                is_unhedged=False,
            )
            self.portfolio.open_positions.append(pos)
            self.portfolio.cash_usdc -= leg.size_usdc

    def _record_arb_failure(self, execution: ArbExecution) -> None:
        from datetime import datetime
        for leg in execution.filled_legs:
            pos = OpenPosition(
                condition_id=execution.opportunity.condition_id,
                question=execution.opportunity.question,
                token_id=leg.token_id,
                direction=leg.outcome_label,
                quantity=leg.size_tokens,
                entry_price=leg.price,
                cost_basis=leg.size_usdc,
                entry_time=datetime.utcnow(),
                arb_type=execution.opportunity.arb_type,
                is_unhedged=True,
            )
            self.portfolio.open_positions.append(pos)
            self.portfolio.cash_usdc -= leg.size_usdc

    async def _close_position(
        self,
        pos: OpenPosition,
        exit_price: float,
        reason: str,
    ) -> None:
        from datetime import datetime
        from .models import OrderSide, OrderType
        await self.orders.place_order(
            token_id=pos.token_id,
            side=OrderSide.SELL,
            price=exit_price,
            size_usdc=pos.quantity * exit_price,
            order_type=OrderType.FOK,
        )
        proceeds = pos.quantity * exit_price
        pnl      = proceeds - pos.cost_basis
        pnl_pct  = pnl / pos.cost_basis if pos.cost_basis else 0

        closed = ClosedPosition(
            condition_id=pos.condition_id, question=pos.question,
            token_id=pos.token_id, direction=pos.direction,
            quantity=pos.quantity, entry_price=pos.entry_price,
            exit_price=exit_price, cost_basis=pos.cost_basis,
            exit_proceeds=round(proceeds, 4), pnl=round(pnl, 4),
            pnl_pct=round(pnl_pct, 6), entry_time=pos.entry_time,
            exit_time=datetime.utcnow(), exit_reason=reason,
            arb_type=pos.arb_type,
        )
        self.portfolio.open_positions.remove(pos)
        self.portfolio.closed_positions.append(closed)
        self.portfolio.cash_usdc += proceeds
        self.metrics.record_closed_position(closed)
        logger.info(f"  [SIM] Closed: {closed}")

    # ── Market filter ─────────────────────────────────────────────────────────

    def _market_passes(self, m: Market) -> bool:
        if not m.is_tradeable:
            return False
        # Skip extreme prices — no meaningful arb at the tails
        if m.yes_token.price < 0.03 or m.yes_token.price > 0.97:
            return False
        return True

    # ── Reporting ─────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        if self.live_data:
            data_line = "  Data            : LIVE — Polymarket Gamma API + CLOB WebSocket\n"
            exec_line = "  Order execution : PAPER (simulated fills — no real orders placed)\n"
        else:
            data_line = "  Data            : SYNTHETIC — fully offline, no network calls\n"
            exec_line = (
                f"  Arb prob/tick   : 10% per market (synthetic injection)\n"
                f"  Market lifetime : ~5 min (10% resolve chance per 30s check)\n"
            )
        logger.info(
            f"\n{'='*65}\n"
            f"  POLYMARKET PAPER TRADING\n"
            f"{'─'*65}\n"
            f"  Capital         : ${self.config.initial_capital:.2f} USDC (paper)\n"
            f"  Max position    : {self.config.max_position_pct*100:.0f}% per market\n"
            f"  Max exposure    : {self.config.max_portfolio_exposure_pct*100:.0f}% total\n"
            f"  Min spread      : {self.config.s2o_min_spread_pct*100:.0f}% (S2O arb)\n"
            f"  Platform fee    : {self.config.taker_fee_rate*100:.0f}% (simulated)\n"
            f"  FOK fill rate   : ~99.6%/leg → 99.2% arb success (Section 6)\n"
            f"  Exec latency    : ~80ms (simulated)\n"
            f"{data_line}"
            f"{exec_line}"
            f"{'='*65}"
        )

    def _print_status(self) -> None:
        equity = self.portfolio.equity
        pnl    = equity - self.config.initial_capital
        sign   = "+" if pnl >= 0 else ""

        logger.info(
            f"\n  PORTFOLIO (SIMULATED)\n"
            f"  Cash     : ${self.portfolio.cash_usdc:.2f}\n"
            f"  Deployed : ${self.portfolio.total_cost_basis:.2f}\n"
            f"  Equity   : ${equity:.2f}  "
            f"({sign}${pnl:.4f} / {sign}{pnl/self.config.initial_capital*100:.2f}%)\n"
            f"  Positions: {len(self.portfolio.open_positions)} open  "
            f"| {len(self.portfolio.closed_positions)} closed\n"
        )
        if self._arb_events:
            logger.info("  Recent arb events:")
            for ev in self._arb_events[:5]:
                logger.info(ev)

    def _print_final_report(self) -> None:
        equity = self.portfolio.equity
        pnl    = equity - self.config.initial_capital
        closed = self.portfolio.closed_positions

        logger.info(f"\n{'='*65}")
        logger.info(f"  SIMULATION COMPLETE")
        logger.info(f"{'='*65}")
        logger.info(f"  Starting capital : ${self.config.initial_capital:.2f}")
        logger.info(f"  Final equity     : ${equity:.2f}")
        logger.info(f"  Total P&L        : ${pnl:+.4f}")
        logger.info(f"  Scan cycles      : {self._scan_count}")
        logger.info(f"  Closed positions : {len(closed)}")
        logger.info(f"  Avg exec latency : {self.orders.avg_execution_ms:.0f}ms")

        if closed:
            wins = [p for p in closed if p.pnl > 0]
            wr   = len(wins) / len(closed) * 100
            logger.info(
                f"  Win rate         : {wr:.1f}% ({len(wins)}W/{len(closed)-len(wins)}L)"
            )

        self.metrics.print_dashboard(equity, self.config.initial_capital)
        logger.info(f"{'='*65}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="simulate",
        description="Polymarket paper trading simulation (fully offline)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python simulate.py                          default: 15 markets, 60s scan, forever
  python simulate.py --scans 30               stop after 30 scan cycles
  python simulate.py --tick 0.25 --markets 20 fast 250ms ticks, 20 markets
  python simulate.py --capital 500            $500 simulated capital
  python simulate.py --seed 99 --scans 20     reproducible run
  python simulate.py --log-level DEBUG        verbose strategy decisions
        """,
    )

    # Data source
    p.add_argument("--live-data", action="store_true",
                   help="Fetch real market data from Polymarket (paper-trade only, no orders placed)")

    # Simulation-only options (ignored when --live-data is set)
    p.add_argument("--tick", type=float, default=1.0, metavar="SECS",
                   help="WebSocket tick interval in seconds (default: 1.0)")
    p.add_argument("--markets", type=int, default=15, metavar="N",
                   help="Number of synthetic markets, max 20 (default: 15)")
    p.add_argument("--seed", type=int, default=42, metavar="N",
                   help="RNG seed for reproducible runs (default: 42)")

    # Bot parameters (same as polymarket_bot.py)
    p.add_argument("--capital", type=float, default=50.0, metavar="USDC",
                   help="Starting capital in USDC (default: 50)")
    p.add_argument("--max-bet", type=float, default=5.0, metavar="USDC",
                   help="Max single bet in USDC (default: 5)")
    p.add_argument("--max-position-pct", type=float, default=5.0, metavar="PCT",
                   help="Max %% per market position (default: 5)")
    p.add_argument("--max-exposure-pct", type=float, default=20.0, metavar="PCT",
                   help="Max %% total portfolio exposure (default: 20)")
    p.add_argument("--min-spread", type=float, default=3.0, metavar="PCT",
                   help="Min spread %% for S2O arb (default: 3)")
    p.add_argument("--taker-fee", type=float, default=2.0, metavar="PCT",
                   help="Simulated taker fee %% (default: 2)")
    p.add_argument("--daily-loss-cap", type=float, default=5.0, metavar="PCT",
                   help="Daily loss cap %% (default: 5)")
    p.add_argument("--circuit-breaker", type=int, default=5, metavar="N",
                   help="Circuit breaker: halt after N consecutive losses (default: 5)")
    p.add_argument("--scan-interval", type=int, default=10, metavar="SEC",
                   help="Seconds between batch scans (default: 10)")
    p.add_argument("--scans", type=int, default=None, metavar="N",
                   help="Stop after N scan cycles (default: run forever)")
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


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)

    if args.markets > 20:
        logging.getLogger(__name__).warning(
            f"--markets capped at 20 (only 20 synthetic market questions available)"
        )
        args.markets = 20

    config = PolymarketConfig(
        initial_capital=args.capital,
        max_bet_usd=args.max_bet,
        max_position_pct=args.max_position_pct / 100.0,
        max_portfolio_exposure_pct=args.max_exposure_pct / 100.0,
        s2o_min_spread_pct=args.min_spread / 100.0,
        taker_fee_rate=args.taker_fee / 100.0,
        daily_loss_cap_pct=args.daily_loss_cap / 100.0,
        circuit_breaker_consecutive_losses=args.circuit_breaker,
        scan_interval_s=args.scan_interval,
        dry_run=True,   # simulation is always paper trading
        log_level=args.log_level,
    )

    bot = SimulationBot(
        config=config,
        live_data=args.live_data,
        tick_interval_s=args.tick,
        num_markets=args.markets,
        seed=args.seed,
    )
    bot.run(max_scans=args.scans)


if __name__ == "__main__":
    main()
