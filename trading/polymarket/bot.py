"""
bot.py — Main async bot orchestrator wiring all 4 Section 3 components.

Section 3: Bot Architecture
────────────────────────────────────────────────────────────────────────────
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Data Collector      2. Strategy Engine    3. Order Manager           │
│  (WebSocket/REST)  ──► (signal generation) ──► (CLOB API)               │
│                                                       │                  │
│                         4. Risk Manager ──────────────┘                  │
│                         (limits & stop-loss)                             │
└─────────────────────────────────────────────────────────────────────────┘

Section 3 APIs:
  Gamma API  → market metadata  (DataCollector.rest)
  CLOB API   → trading          (OrderManager → auth.clob_client)
  Data API   → positions/hist.  (DataCollector.rest.get_positions)

Event loop tasks
────────────────
  ws_task      — WebSocket connection (reconnects automatically)
  scan_task    — Every scan_interval_s: fetch markets, subscribe new tokens,
                 run batch REST scan for early opportunities
  monitor_task — Every position_check_interval_s: check open positions
                 for stop conditions, update equity curve in metrics

Signal flow (hot path, ≤ 80 ms target)
────────────────────────────────────────
  WS book update
    → DataCollector.ws._on_snapshot/_on_delta
    → bot._on_book_update()               (rate-limited per token)
    → StrategyEngine.analyze_market()     (O(1), no I/O)
    → RiskManager.approve()
    → OrderManager.execute_arb()          (FOK, both legs)
    → MetricsTracker.record_arb_execution()
    → portfolio updated
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Set

from .auth import PolymarketAuth
from .config import PolymarketConfig
from .data_collector import DataCollector
from .metrics import MetricsTracker
from .models import (
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
from .order_manager import OrderManager
from .risk_manager import RiskManager
from .strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)


class PolymarketBot:
    """
    Polymarket automated trading bot — rebuilt per infrastructure guide.

    Parameters
    ----------
    config      : Runtime configuration (Section 1–6 parameters).
    private_key : Polygon wallet private key.
                  Required only when config.dry_run = False.

    DISCLAIMER
    ----------
    Prediction-market trading involves real financial risk.
    Arbitrage windows are seconds wide — spreads can close before execution.
    Default dry_run=True.  Validate extensively before enabling live trading.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        private_key: Optional[str] = None,
    ) -> None:
        self.config = config

        # Section 1: Auth (two-tier L1 + L2)
        self._auth: Optional[PolymarketAuth] = None
        if private_key and not config.dry_run:
            self._auth = PolymarketAuth(
                private_key=private_key,
                chain_id=config.chain_id,
                sig_type=config.sig_type,
            )

        # Section 3, Components 1-4
        self.data      = DataCollector(config=config, on_book_update=self._on_book_update)
        self.strategy  = StrategyEngine(config=config)
        self.orders    = OrderManager(config=config, auth=self._auth)
        self.risk      = RiskManager(config=config)
        self.metrics   = MetricsTracker(config=config)

        # Portfolio
        self.portfolio = PortfolioState(cash_usdc=config.initial_capital)

        # Internal state
        self._markets: Dict[str, Market] = {}         # condition_id → Market
        self._tid_to_cid: Dict[str, str] = {}         # token_id → condition_id
        self._book_ts: Dict[str, float] = {}          # token_id → last signal time
        self._recent_attempts: Dict[str, float] = {}  # condition_id → monotonic time of last attempt
        self._scan_count = 0
        self._stop = False

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self, max_scans: Optional[int] = None) -> None:
        """Start the bot (synchronous wrapper). Blocks until stopped."""
        try:
            asyncio.run(self._run_async(max_scans=max_scans))
        except KeyboardInterrupt:
            logger.info("Bot stopped by user (Ctrl+C)")

    async def _run_async(self, max_scans: Optional[int] = None) -> None:
        self._print_banner()

        # Initialise authentication (skipped in dry-run)
        if self._auth:
            self._auth.initialize(self.config.clob_host)
            balance = self._get_live_balance()
            if balance > 0:
                self.portfolio.cash_usdc = balance
                logger.info(f"Live USDC balance: ${balance:.2f}")

        # Launch 3 concurrent async tasks
        ws_task      = asyncio.create_task(self.data.ws.connect(), name="ws_feed")
        scan_task    = asyncio.create_task(self._scan_loop(max_scans), name="scanner")
        monitor_task = asyncio.create_task(self._monitor_loop(), name="monitor")

        try:
            await asyncio.gather(scan_task, monitor_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._stop = True
            ws_task.cancel()
            monitor_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(ws_task, monitor_task, return_exceptions=True),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                pass
            self._print_final_report()

    # ── Task 1: Market scanner ────────────────────────────────────────────────

    async def _scan_loop(self, max_scans: Optional[int] = None) -> None:
        """
        Periodic market scan.

        1. Fetch active markets from Gamma API.
        2. Register new markets and subscribe their tokens to the WS feed.
        3. Run a REST-based batch scan for early opportunities (before WS warms up).
        4. Log status.
        """
        while not self._stop:
            self._scan_count += 1
            t0 = time.monotonic()

            logger.info(
                f"\n{'─'*65}"
                f"\n  Scan #{self._scan_count}  —  "
                f"{datetime.utcnow():%Y-%m-%d %H:%M:%S UTC}"
                f"\n  Risk: {self.risk.status()}"
                f"\n{'─'*65}"
            )

            # Fetch + filter markets
            all_markets = self.data.get_active_markets(limit=200)
            tradeable   = [m for m in all_markets if self._market_passes(m)]
            logger.info(f"Markets: {len(all_markets)} total → {len(tradeable)} tradeable")

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
                logger.info(f"WS: subscribed {len(new_tids)} new tokens")

            # Prune stale dedup entries so the dict doesn't grow unbounded
            now = time.monotonic()
            self._recent_attempts = {
                cid: ts for cid, ts in self._recent_attempts.items()
                if now - ts < self.config.attempt_cooldown_s
            }

            # REST batch scan (catches opportunities before WS warms up)
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

            scan_ms = (time.monotonic() - t0) * 1000
            logger.debug(f"Scan took {scan_ms:.0f}ms")

            if max_scans and self._scan_count >= max_scans:
                logger.info(f"Reached max_scans={max_scans} — stopping.")
                return

            logger.info(f"Next scan in {self.config.scan_interval_s}s  "
                        f"| WS alive: {self.data.ws.is_connected}")
            await asyncio.sleep(self.config.scan_interval_s)

    # ── Task 2: Position monitor ──────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """
        Periodically check open positions for stop conditions.
        For arb positions (both legs filled), this is mainly a safety check —
        the position pays out at resolution and doesn't need a stop-loss per se.
        For endgame / single-leg positions, we exit if the probability drops.
        """
        while not self._stop:
            await asyncio.sleep(self.config.position_check_interval_s)
            for pos in list(self.portfolio.open_positions):
                book = self.data.get_book(pos.token_id)
                if book is None or book.best_bid is None:
                    continue
                current_price = book.best_bid
                pnl_pct = pos.unrealized_pnl(current_price) / pos.cost_basis if pos.cost_basis else 0

                # For unhedged legs (one leg of arb failed), cut quickly
                if pos.is_unhedged and pnl_pct < -0.10:
                    logger.warning(
                        f"Cutting unhedged position {pos.direction} "
                        f"pnl={pnl_pct*100:.1f}%"
                    )
                    await self._close_position(pos, current_price, "unhedged_stop")
                    continue

                # For endgame: exit if probability reversed significantly
                if pos.arb_type == ArbType.ENDGAME and pnl_pct < -0.05:
                    logger.warning(
                        f"Endgame position reversed {pnl_pct*100:.1f}% — closing"
                    )
                    await self._close_position(pos, current_price, "endgame_stop")

    # ── WebSocket callback (hot path) ─────────────────────────────────────────

    async def _on_book_update(
        self,
        token_id: str,
        book: OrderBookSnapshot,
    ) -> None:
        """
        Called on every WebSocket order-book update.
        This is the hot path — must be fast (target < 5ms for strategy analysis).
        Rate-limited per token to avoid overloading the strategy engine.
        """
        if self._stop or self.risk.state.trading_halted:
            return

        # Rate limit: max one signal per token per signal_cooldown_s
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
        """Run the full risk-check + execute + portfolio-update cycle."""
        # Deduplication: the WS callback and the batch scanner can both detect
        # the same opportunity if a spread persists across a scan cycle.
        # Skip if we already attempted this market within the cooldown window.
        now = time.monotonic()
        last = self._recent_attempts.get(opp.condition_id, 0.0)
        if now - last < self.config.attempt_cooldown_s:
            logger.debug(
                f"Dedup: skipping {opp.condition_id[:8]} "
                f"(attempted {now - last:.1f}s ago)"
            )
            return
        self._recent_attempts[opp.condition_id] = now

        if not self.risk.approve(opp, self.portfolio):
            return

        logger.info(f"\n  OPPORTUNITY: {opp}")

        t0 = time.monotonic()
        execution: ArbExecution = await self.orders.execute_arb(opp)
        elapsed_ms = (time.monotonic() - t0) * 1000

        # Update risk manager
        self.risk.record_execution(execution, self.portfolio)

        # Update metrics
        self.metrics.record_arb_execution(execution)

        # Update portfolio
        if execution.result == ArbResult.SUCCESS:
            self._record_arb_success(execution)
        elif execution.result in (ArbResult.PARTIAL_UNWIND, ArbResult.FAIL_UNWIND):
            self._record_arb_failure(execution)

    def _record_arb_success(self, execution: ArbExecution) -> None:
        """For sum-to-one arb: both legs open; payout received at resolution."""
        opp = execution.opportunity

        for leg in execution.filled_legs:
            # For multi-leg arb, record both legs as open positions
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
        """Record any remaining unhedged exposure after a partial failure."""
        for leg in execution.filled_legs:
            # The unwind already tried to close these, but if it failed, record
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
                is_unhedged=True,  # flag for monitor to cut quickly
            )
            self.portfolio.open_positions.append(pos)
            self.portfolio.cash_usdc -= leg.size_usdc

    async def _close_position(
        self,
        pos: OpenPosition,
        exit_price: float,
        reason: str,
    ) -> None:
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
        logger.info(f"  Closed: {closed}")

    # ── Market filter ─────────────────────────────────────────────────────────

    def _market_passes(self, m: Market) -> bool:
        if not m.is_tradeable:
            return False
        if m.liquidity < self.config.min_liquidity_usd:
            return False
        if m.volume_24h < self.config.min_volume_24h_usd:
            return False
        # Price must leave room for the spread to be exploitable
        if m.yes_token.price < 0.03 or m.yes_token.price > 0.97:
            return False
        return True

    # ── Balance sync ──────────────────────────────────────────────────────────

    def _get_live_balance(self) -> float:
        if not self._auth or not self._auth.is_ready:
            return 0.0
        try:
            return float(self._auth.clob_client.get_balance().get("balance", 0))
        except Exception:
            return 0.0

    # ── Kill switch (callable externally) ────────────────────────────────────

    def emergency_stop(self, reason: str = "manual") -> None:
        """Immediately halt all trading. Call this from a signal handler."""
        self.risk.activate_kill_switch(reason)
        self._stop = True

    # ── Reporting ─────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        logger.info(
            f"\n{'='*65}\n"
            f"  POLYMARKET AUTOMATED TRADING BOT  v2.0\n"
            f"  (Rebuilt per Infrastructure Guide)\n"
            f"{'─'*65}\n"
            f"  Capital         : ${self.config.initial_capital:.2f} USDC\n"
            f"  Max position    : {self.config.max_position_pct*100:.0f}% per market\n"
            f"  Max exposure    : {self.config.max_portfolio_exposure_pct*100:.0f}% total\n"
            f"  Min spread      : {self.config.s2o_min_spread_pct*100:.0f}% (S2O arb)\n"
            f"  Platform fee    : {self.config.taker_fee_rate*100:.0f}% (taker)\n"
            f"  Daily loss cap  : {self.config.daily_loss_cap_pct*100:.0f}%\n"
            f"  Circuit breaker : {self.config.circuit_breaker_consecutive_losses} losses\n"
            f"  Rate limits     : {self.config.public_rate_limit_per_min} req/min  "
            f"|  {self.config.trading_rate_limit_per_min} orders/min\n"
            f"  WS ping (CLOB)  : {self.config.clob_ws_ping_s}s\n"
            f"  Mode            : {'DRY RUN (paper trading)' if self.config.dry_run else 'LIVE TRADING'}\n"
            f"{'='*65}"
        )

    def _print_status(self) -> None:
        equity = self.portfolio.equity
        pnl    = equity - self.config.initial_capital
        sign   = "+" if pnl >= 0 else ""

        logger.info(
            f"\n  PORTFOLIO\n"
            f"  Cash     : ${self.portfolio.cash_usdc:.2f}\n"
            f"  Deployed : ${self.portfolio.total_cost_basis:.2f}\n"
            f"  Equity   : ${equity:.2f}  ({sign}${pnl:.4f} / {sign}{pnl/self.config.initial_capital*100:.2f}%)\n"
            f"  Positions: {len(self.portfolio.open_positions)} open  "
            f"| {len(self.portfolio.closed_positions)} closed\n"
            f"  WS       : {'connected' if self.data.ws.is_connected else 'DISCONNECTED'}"
        )
        if self.portfolio.open_positions:
            for p in self.portfolio.open_positions:
                logger.info(f"    {p}")

    def _print_final_report(self) -> None:
        equity = self.portfolio.equity
        pnl    = equity - self.config.initial_capital
        closed = self.portfolio.closed_positions

        logger.info(f"\n{'='*65}")
        logger.info(f"  FINAL REPORT")
        logger.info(f"{'='*65}")
        logger.info(f"  Starting capital : ${self.config.initial_capital:.2f}")
        logger.info(f"  Final equity     : ${equity:.2f}")
        logger.info(f"  Total P&L        : ${pnl:+.4f}")
        logger.info(f"  Scan cycles      : {self._scan_count}")
        logger.info(f"  Closed positions : {len(closed)}")

        if closed:
            wins = [p for p in closed if p.pnl > 0]
            wr   = len(wins) / len(closed) * 100
            logger.info(f"  Win rate         : {wr:.1f}% ({len(wins)}W/{len(closed)-len(wins)}L)")

        self.metrics.print_dashboard(equity, self.config.initial_capital)
        logger.info(f"{'='*65}")
