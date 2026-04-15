"""
order_manager.py — Section 3, Component 3: Order Manager (CLOB API).

Responsibilities
----------------
• Place FOK, GTC, and GTD orders via the Polymarket CLOB API (py_clob_client).
• Enforce Section 1 trading rate limit: 60 orders/min.
• Execute multi-leg arbitrage atomically:
    1. Submit first leg (FOK).
    2. If filled → immediately submit second leg (FOK).
    3. If second leg fails → unwind first leg at market.
    4. Track execution time for the ~80ms response-time target (Section 6).
• Dry-run mode: log what would happen without real network calls.

Order types (Section 5: Execution)
────────────────────────────────────
FOK (Fill-or-Kill)   All or nothing; expires immediately if not fully filled.
                     Used for arbitrage — ensures atomic execution.
GTC (Good-Till-Cancelled)  Rests in the order book until filled or cancelled.
                     Used for endgame plays where patience is acceptable.
GTD (Good-Till-Date) Expires at a specified timestamp.
                     Used when you want time-bounded passive orders.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import List, Optional, Tuple

from .config import PolymarketConfig
from .models import (
    ArbExecution,
    ArbLeg,
    ArbOpportunity,
    ArbResult,
    OrderSide,
    OrderType,
    PlacedOrder,
)

logger = logging.getLogger(__name__)


# ── Trading rate limiter (60 orders/min, Section 1) ───────────────────────────

class TradingRateLimiter:
    """Token-bucket for Section 1: 60 orders/min (trading)."""

    def __init__(self, max_per_minute: int = 60) -> None:
        self._max = max_per_minute
        self._window: deque = deque()

    async def acquire(self) -> None:
        now = time.monotonic()
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()

        if len(self._window) >= self._max:
            wait = 60.0 - (now - self._window[0]) + 0.05
            logger.debug(f"Trade rate limit: waiting {wait:.2f}s")
            await asyncio.sleep(wait)
            now = time.monotonic()
            while self._window and now - self._window[0] > 60.0:
                self._window.popleft()

        self._window.append(time.monotonic())


# ── Order Manager ─────────────────────────────────────────────────────────────

class OrderManager:
    """
    Section 3, Component 3: Order Manager.

    Central execution layer — handles all order types and multi-leg arb.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        auth=None,       # PolymarketAuth (or None in dry-run)
    ) -> None:
        self.config  = config
        self.auth    = auth
        self._rl     = TradingRateLimiter(config.trading_rate_limit_per_min)
        self._orders: List[PlacedOrder] = []    # order log

    # ── Single-order placement ─────────────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size_usdc: float,
        order_type: OrderType = OrderType.FOK,
        expiry_ts: Optional[int] = None,         # for GTD: Unix timestamp
    ) -> PlacedOrder:
        """
        Place a single order on the CLOB.

        Returns a PlacedOrder with status FILLED / CANCELLED / EXPIRED.
        In dry-run mode, always returns FILLED (optimistic simulation).
        """
        await self._rl.acquire()

        size_tokens = size_usdc / price if price > 0 else 0.0
        t0 = time.monotonic()

        if self.config.dry_run:
            order_id = f"DRY-{order_type.value}-{int(t0*1000)}"
            elapsed  = (time.monotonic() - t0) * 1000
            logger.info(
                f"[DRY RUN] {order_type.value:3s}  {side.value:4s}  "
                f"token={token_id[:16]}  price={price:.4f}  "
                f"size=${size_usdc:.2f} ({size_tokens:.4f}tok)  "
                f"id={order_id}  ({elapsed:.1f}ms)"
            )
            order = PlacedOrder(
                order_id=order_id,
                token_id=token_id,
                side=side,
                order_type=order_type,
                price=price,
                size_tokens=size_tokens,
                size_usdc=size_usdc,
                filled_size=size_tokens,   # dry-run: assume full fill
                status="FILLED",
            )
            self._orders.append(order)
            return order

        # Live trading via py_clob_client
        return await self._place_live(
            token_id, side, price, size_tokens, size_usdc, order_type, expiry_ts, t0
        )

    async def _place_live(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size_tokens: float,
        size_usdc: float,
        order_type: OrderType,
        expiry_ts: Optional[int],
        t0: float,
    ) -> PlacedOrder:
        """Submit an order to the live CLOB using py_clob_client."""
        clob = self.auth.clob_client

        for attempt in range(1, self.config.max_api_retries + 1):
            try:
                from py_clob_client.clob_types import OrderArgs
                from py_clob_client.order_builder.constants import BUY, SELL as CLOB_SELL

                clob_side = BUY if side == OrderSide.BUY else CLOB_SELL

                # Map our order type to CLOB order type flags
                extra_kwargs = {}
                if order_type == OrderType.FOK:
                    extra_kwargs["time_in_force"] = "FOK"
                elif order_type == OrderType.GTC:
                    extra_kwargs["time_in_force"] = "GTC"
                elif order_type == OrderType.GTD:
                    extra_kwargs["time_in_force"] = "GTD"
                    if expiry_ts:
                        extra_kwargs["expiration"] = expiry_ts

                order_args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 4),
                    size=round(size_tokens, 4),
                    side=clob_side,
                    **extra_kwargs,
                )
                signed   = clob.create_order(order_args)
                response = clob.post_order(signed)

                elapsed_ms = (time.monotonic() - t0) * 1000
                order_id   = response.get("orderID") or response.get("order_id", "")
                status     = response.get("status", "FILLED").upper()
                filled     = float(response.get("size_matched", size_tokens))

                logger.info(
                    f"{order_type.value:3s}  {side.value:4s}  "
                    f"price={price:.4f}  size=${size_usdc:.2f}  "
                    f"status={status}  id={order_id}  ({elapsed_ms:.1f}ms)"
                )

                order = PlacedOrder(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    order_type=order_type,
                    price=price,
                    size_tokens=size_tokens,
                    size_usdc=size_usdc,
                    filled_size=filled,
                    status=status,
                )
                self._orders.append(order)
                return order

            except Exception as exc:
                delay = self.config.api_retry_delay_s * (2 ** (attempt - 1))
                logger.warning(f"Order attempt {attempt}: {exc}")
                if attempt < self.config.max_api_retries:
                    await asyncio.sleep(delay)

        # All retries exhausted
        return PlacedOrder(
            order_id="FAILED",
            token_id=token_id, side=side, order_type=order_type,
            price=price, size_tokens=size_tokens, size_usdc=size_usdc,
            filled_size=0.0, status="FAILED",
        )

    # ── Multi-leg arbitrage execution ─────────────────────────────────────────

    async def execute_arb(
        self,
        opportunity: ArbOpportunity,
    ) -> ArbExecution:
        """
        Execute a multi-leg arbitrage opportunity.

        Section 4 note: "Arbitrage windows last only seconds."
        We target ≤ 80ms total execution time (Section 6).

        Execution flow for a 2-leg Sum-to-One arb:
        ─────────────────────────────────────────────
        1. Submit Leg 1 (YES) as FOK.
        2a. If Leg 1 FILLED → immediately submit Leg 2 (NO) as FOK.
            2b. If Leg 2 FILLED → SUCCESS, arb locked in.
            2c. If Leg 2 NOT FILLED → UNWIND Leg 1 (sell immediately).
        2b. If Leg 1 NOT FILLED → MISS (no exposure, try again next tick).

        For single-leg arbs (endgame), just submit the one leg.
        """
        t0 = time.monotonic()
        filled_legs: List[ArbLeg] = []
        realized_profit = 0.0
        slippage = 0.0

        if len(opportunity.legs) == 1:
            # Single-leg (endgame, cross-platform)
            result, filled, slippage = await self._execute_single_leg(opportunity.legs[0])
            if result == ArbResult.SUCCESS:
                filled_legs.append(filled)
                realized_profit = filled.size_usdc * opportunity.net_profit_pct
        else:
            # Multi-leg (sum-to-one, combinatorial)
            result, filled_legs, realized_profit, slippage = await self._execute_multi_leg(
                opportunity.legs
            )

        elapsed_ms = (time.monotonic() - t0) * 1000

        # Log result
        sign = "+" if realized_profit >= 0 else ""
        logger.info(
            f"ARB {result.value:15s}  {opportunity.arb_type.value:14s}  "
            f"{opportunity.question[:40]:40s}  "
            f"profit={sign}${realized_profit:.4f}  "
            f"slippage=${slippage:.4f}  time={elapsed_ms:.1f}ms"
        )

        # Warn if we exceeded the 80ms target
        if elapsed_ms > self.config.target_response_time_ms * 2:
            logger.warning(
                f"Execution time {elapsed_ms:.0f}ms exceeded "
                f"2× target ({self.config.target_response_time_ms*2:.0f}ms)"
            )

        return ArbExecution(
            opportunity=opportunity,
            result=result,
            filled_legs=filled_legs,
            realized_profit_usd=round(realized_profit, 6),
            slippage_usd=round(slippage, 6),
            execution_time_ms=round(elapsed_ms, 2),
        )

    async def _execute_single_leg(
        self, leg: ArbLeg
    ) -> Tuple[ArbResult, ArbLeg, float]:
        order = await self.place_order(
            token_id=leg.token_id,
            side=leg.side,
            price=leg.price,
            size_usdc=leg.size_usdc,
            order_type=leg.order_type,
        )

        if order.status != "FILLED" or order.filled_size < leg.size_tokens * 0.95:
            return ArbResult.MISS, leg, 0.0

        # Compute slippage (fill price vs expected price)
        fill_price = order.size_usdc / order.filled_size if order.filled_size else leg.price
        slippage   = abs(fill_price - leg.price) * order.filled_size

        filled_leg = ArbLeg(
            token_id=leg.token_id,
            outcome_label=leg.outcome_label,
            side=leg.side,
            price=fill_price,
            size_tokens=order.filled_size,
            size_usdc=order.size_usdc,
            order_type=leg.order_type,
        )
        return ArbResult.SUCCESS, filled_leg, slippage

    async def _execute_multi_leg(
        self,
        legs: List[ArbLeg],
    ) -> Tuple[ArbResult, List[ArbLeg], float, float]:
        """Execute legs sequentially; unwind on failure of any leg."""
        filled: List[ArbLeg] = []
        total_slip = 0.0

        for i, leg in enumerate(legs):
            order = await self.place_order(
                token_id=leg.token_id,
                side=leg.side,
                price=leg.price,
                size_usdc=leg.size_usdc,
                order_type=leg.order_type,
            )

            if order.status != "FILLED" or order.filled_size < leg.size_tokens * 0.95:
                # This leg failed — unwind all previously filled legs
                if filled:
                    unwind_ok = await self._unwind(filled)
                    return (
                        ArbResult.PARTIAL_UNWIND if unwind_ok else ArbResult.FAIL_UNWIND,
                        filled, 0.0, total_slip,
                    )
                return ArbResult.MISS, [], 0.0, 0.0

            fill_price = order.size_usdc / order.filled_size if order.filled_size else leg.price
            total_slip += abs(fill_price - leg.price) * order.filled_size

            filled.append(ArbLeg(
                token_id=leg.token_id,
                outcome_label=leg.outcome_label,
                side=leg.side,
                price=fill_price,
                size_tokens=order.filled_size,
                size_usdc=order.size_usdc,
                order_type=leg.order_type,
            ))

        # All legs filled
        # For sum-to-one: realized profit = payout(1.0/token) − total_cost
        total_cost    = sum(l.size_usdc for l in filled)
        total_tokens  = min(l.size_tokens for l in filled)  # equal payout K tokens
        realized_prof = total_tokens * 1.0 - total_cost      # $1 payout per token

        return ArbResult.SUCCESS, filled, realized_prof, total_slip

    async def _unwind(self, filled_legs: List[ArbLeg]) -> bool:
        """Sell back all filled legs at market (bid price) to eliminate exposure."""
        all_ok = True
        for leg in filled_legs:
            logger.warning(
                f"UNWIND: SELL {leg.outcome_label}  "
                f"qty={leg.size_tokens:.4f}  (emergency close)"
            )
            order = await self.place_order(
                token_id=leg.token_id,
                side=OrderSide.SELL,
                price=max(0.01, leg.price * 0.95),   # accept 5% slippage to close
                size_usdc=leg.size_usdc,
                order_type=OrderType.FOK,
            )
            if order.status != "FILLED":
                logger.error(f"Unwind FAILED for {leg.outcome_label} — directional exposure!")
                all_ok = False
        return all_ok

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a GTC/GTD order."""
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Cancel {order_id}")
            return True
        if not self.auth or not self.auth.is_ready:
            return False
        try:
            self.auth.clob_client.cancel_order(order_id)
            logger.info(f"Cancelled {order_id}")
            return True
        except Exception as exc:
            logger.warning(f"Cancel failed ({order_id}): {exc}")
            return False

    def get_order_log(self) -> List[PlacedOrder]:
        return list(self._orders)
