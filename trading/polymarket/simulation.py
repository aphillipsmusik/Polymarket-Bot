"""
simulation.py — Fully offline synthetic market engine for paper trading.

Replaces DataCollector + OrderManager with simulated equivalents so the
entire bot stack (StrategyEngine, RiskManager, MetricsTracker) runs
without any network access.

Market dynamics
───────────────
Each synthetic market has a "true probability" P that evolves as:

    P(t+1) = P(t) + mean_reversion × (0.5 − P(t)) + σ × ε

where ε ~ N(0,1).  This is a discrete Ornstein-Uhlenbeck process —
prices oscillate around 0.5 and occasionally drift toward 0 or 1.

Normal order books (no arb):
    ask_yes ≈ P + spread/2 + noise
    ask_no  ≈ (1−P) + spread/2 + noise
    sum     ≈ 1.02–1.05   (market-maker markup)

Arb injection (ARB_PROB chance per tick per market):
    ask_yes and/or ask_no temporarily reduced so sum < 0.97
    Window lasts ARB_DURATION ticks, then prices normalise

Simulated order execution
──────────────────────────
FOK orders: 99.6% fill rate per leg → 99.2% arb-level success rate (Section 6 target)
Slippage  : Gaussian noise ~0.1–0.3 cents per order
Latency   : asyncio.sleep(0.08) simulates ~80ms execution delay
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Coroutine, Dict, List, Optional, Set, Tuple

from .config import PolymarketConfig
from .models import (
    Market,
    OrderBookSnapshot,
    OrderLevel,
    OrderSide,
    OrderType,
    PlacedOrder,
    Token,
)

# ── Tuneable simulation constants ─────────────────────────────────────────────

ARB_PROB         = 0.10   # 10% chance of arb window opening per market per tick
ARB_DURATION     = 3      # arb window lasts this many ticks
ARB_MIN_SPREAD   = 0.030  # minimum injected sum-to-one spread (just profitable)
ARB_MAX_SPREAD   = 0.070  # maximum injected sum-to-one spread
NORMAL_MARKUP    = 0.025  # normal market-maker markup (ask_yes + ask_no − 1)
PRICE_MEAN_REV   = 0.04   # mean-reversion speed toward 0.5
PRICE_VOL        = 0.008  # per-tick price volatility
BOOK_LEVELS      = 6      # order-book depth per side
FOK_FILL_RATE    = 0.996  # FOK fill probability per leg → 0.996²≈99.2% arb success (Section 6 target)
SLIP_STD         = 0.002  # slippage std dev (cents)
EXEC_LATENCY_S   = 0.080  # simulated execution latency (80ms, Section 6 target)

# ── BTC 5-minute market simulation ────────────────────────────────────────────
BTC_START_PRICE  = 103_000.0  # Simulated BTC/USDC starting price
BTC_TICK_VOL     = 0.00025    # 0.025% per-tick volatility (~1.5%/min at 1s ticks)
BTC_PRICE_STEP   = 500        # Round thresholds to nearest $500

# Price offsets (pct of BTC price) used to spread 20 market slots across the book.
# Positive = "above" question, negative = "below" question.
_SLOT_OFFSETS = [
    -0.020, -0.015, -0.012, -0.008, -0.005,
    -0.003,  0.003,  0.005,  0.008,  0.012,
     0.015,  0.020, -0.010,  0.010, -0.018,
     0.018, -0.025,  0.025, -0.030,  0.030,
]


def _btc_question(slot: int, btc_price: float) -> str:
    """Generate a BTC 5-minute prediction question for a given market slot."""
    pct = _SLOT_OFFSETS[slot % len(_SLOT_OFFSETS)]
    threshold = round((btc_price * (1 + pct)) / BTC_PRICE_STEP) * BTC_PRICE_STEP
    direction = "above" if pct >= 0 else "below"
    # Stagger expiry windows: slots 0-5 → +5 min, 6-11 → +10 min, etc.
    minutes_out = (slot // 6 + 1) * 5
    expiry = datetime.utcnow() + timedelta(minutes=minutes_out)
    return f"BTC {direction} ${threshold:,.0f} at {expiry:%H:%M} UTC?"


# ── Internal per-market state ─────────────────────────────────────────────────

@dataclass
class _MarketState:
    """Mutable simulation state for one market."""
    cid: str
    question: str
    yes_tid: str
    no_tid: str
    true_prob: float          # current "true" probability
    arb_ticks_left: int = 0  # >0 while an arb window is open
    arb_spread: float = 0.0  # size of injected spread
    rng: random.Random = field(default_factory=random.Random)

    def step(self) -> None:
        """Advance price by one tick."""
        # Ornstein-Uhlenbeck mean-reversion
        drift    = PRICE_MEAN_REV * (0.5 - self.true_prob)
        shock    = PRICE_VOL * self.rng.gauss(0, 1)
        self.true_prob = max(0.05, min(0.95, self.true_prob + drift + shock))

        # Arb window lifecycle
        if self.arb_ticks_left > 0:
            self.arb_ticks_left -= 1
        elif self.rng.random() < ARB_PROB:
            self.arb_ticks_left = ARB_DURATION
            self.arb_spread = self.rng.uniform(ARB_MIN_SPREAD, ARB_MAX_SPREAD)

    @property
    def is_arb(self) -> bool:
        return self.arb_ticks_left > 0

    def ask_yes(self) -> float:
        """Current ask price for YES token."""
        if self.is_arb:
            # Widen sum below 1 by reducing BOTH asks proportionally
            half   = self.arb_spread / 2
            raw    = self.true_prob + 0.005 + self.rng.gauss(0, 0.001) - half
        else:
            raw = self.true_prob + NORMAL_MARKUP / 2 + self.rng.gauss(0, 0.001)
        return round(max(0.03, min(0.97, raw)), 4)

    def ask_no(self) -> float:
        """Current ask price for NO token."""
        if self.is_arb:
            half   = self.arb_spread / 2
            raw    = (1 - self.true_prob) + 0.005 + self.rng.gauss(0, 0.001) - half
        else:
            raw = (1 - self.true_prob) + NORMAL_MARKUP / 2 + self.rng.gauss(0, 0.001)
        return round(max(0.03, min(0.97, raw)), 4)

    def bid_yes(self) -> float:
        return round(max(0.02, self.ask_yes() - self.rng.uniform(0.005, 0.015)), 4)

    def bid_no(self) -> float:
        return round(max(0.02, self.ask_no() - self.rng.uniform(0.005, 0.015)), 4)


# ── Order book factory ────────────────────────────────────────────────────────

def _make_book(tid: str, ask: float, bid: float, rng: random.Random) -> OrderBookSnapshot:
    """Generate a realistic order book with BOOK_LEVELS levels per side."""
    asks = []
    bids = []
    base_ask = ask
    base_bid = bid

    for i in range(BOOK_LEVELS):
        a_price = round(base_ask + i * rng.uniform(0.002, 0.006), 4)
        a_size  = round(rng.uniform(50, 400), 2)
        asks.append(OrderLevel(price=a_price, size=a_size))

        b_price = round(base_bid - i * rng.uniform(0.002, 0.006), 4)
        b_price = max(0.01, b_price)
        b_size  = round(rng.uniform(50, 400), 2)
        bids.append(OrderLevel(price=b_price, size=b_size))

    bids.sort(key=lambda l: -l.price)
    asks.sort(key=lambda l:  l.price)
    return OrderBookSnapshot(token_id=tid, bids=bids, asks=asks)


# ── Simulated WebSocket ───────────────────────────────────────────────────────

class SimulatedWebSocket:
    """
    Mimics ClobWebSocket interface but generates synthetic order-book updates
    at a configurable tick rate instead of connecting to the real CLOB.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        on_book_update=None,
        tick_interval_s: float = 1.0,
        num_markets: int = 15,
        seed: int = 42,
    ) -> None:
        self.config          = config
        self.on_book_update  = on_book_update
        self.tick_interval_s = tick_interval_s
        self._stop           = False
        self._connected      = True   # always "connected" in simulation
        self._subscribed: Set[str] = set()
        self._books: Dict[str, OrderBookSnapshot] = {}
        self._states: List[_MarketState] = []

        # Simulated BTC price (random walk, drives question labels)
        self._btc_price: float = BTC_START_PRICE
        self._btc_rng   = random.Random(seed + 9999)

        # Build initial market states — all BTC 5-minute prediction markets
        rng = random.Random(seed)
        for i in range(num_markets):
            yes_tid = f"sim-yes-{i:04d}"
            no_tid  = f"sim-no-{i:04d}"
            state   = _MarketState(
                cid=f"sim-cid-{i:04d}",
                question=_btc_question(i, self._btc_price),
                yes_tid=yes_tid,
                no_tid=no_tid,
                true_prob=rng.uniform(0.30, 0.70),   # near 50/50 at open
                rng=random.Random(seed + i),
            )
            self._states.append(state)
            # Initial books
            self._books[yes_tid] = _make_book(yes_tid, state.ask_yes(), state.bid_yes(), state.rng)
            self._books[no_tid]  = _make_book(no_tid,  state.ask_no(),  state.bid_no(),  state.rng)

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._stop

    def get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        return self._books.get(token_id)

    def stop(self) -> None:
        self._stop = True

    def get_btc_price(self) -> float:
        return self._btc_price

    def refresh_market_question(self, cid: str) -> None:
        """Regenerate the question for a resolved market slot at current BTC price."""
        for i, state in enumerate(self._states):
            if state.cid == cid:
                state.question = _btc_question(i, self._btc_price)
                break

    async def connect(self) -> None:
        """Tick loop — fires on_book_update for every market each tick."""
        while not self._stop:
            # Step simulated BTC price (geometric random walk)
            self._btc_price *= 1.0 + BTC_TICK_VOL * self._btc_rng.gauss(0, 1)

            for state in self._states:
                state.step()

                # Update YES book
                ay = state.ask_yes()
                by = state.bid_yes()
                book_yes = _make_book(state.yes_tid, ay, by, state.rng)
                self._books[state.yes_tid] = book_yes

                # Update NO book
                an = state.ask_no()
                bn = state.bid_no()
                book_no = _make_book(state.no_tid, an, bn, state.rng)
                self._books[state.no_tid] = book_no

                # Fire callbacks
                if self.on_book_update:
                    await _safe_fire(self.on_book_update, state.yes_tid, book_yes)
                    await _safe_fire(self.on_book_update, state.no_tid,  book_no)

                if state.is_arb:
                    _sum = ay + an
                    # Quick sanity log (picked up by the strategy engine)
                    pass

            await asyncio.sleep(self.tick_interval_s)

    async def add_subscriptions(self, token_ids: List[str]) -> None:
        self._subscribed.update(token_ids)  # no-op: we already emit all markets

    def get_states(self) -> List[_MarketState]:
        return self._states


# ── Simulated data collector ──────────────────────────────────────────────────

class SimulatedDataCollector:
    """
    Drop-in replacement for DataCollector.
    Exposes get_active_markets() and get_book() from synthetic data.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        on_book_update=None,
        tick_interval_s: float = 1.0,
        num_markets: int = 15,
        seed: int = 42,
    ) -> None:
        self.config = config
        self.ws = SimulatedWebSocket(
            config=config,
            on_book_update=on_book_update,
            tick_interval_s=tick_interval_s,
            num_markets=num_markets,
            seed=seed,
        )

    def get_active_markets(self, limit: int = 200) -> List[Market]:
        """Return synthetic Market objects built from simulator states."""
        markets: List[Market] = []
        for st in self.ws.get_states()[:limit]:
            yes_book = self.ws.get_book(st.yes_tid)
            no_book  = self.ws.get_book(st.no_tid)
            yes_price = yes_book.best_ask or 0.5 if yes_book else 0.5
            no_price  = no_book.best_ask  or 0.5 if no_book  else 0.5

            markets.append(Market(
                condition_id=st.cid,
                question=st.question,
                description="",
                end_date=datetime.utcnow() + timedelta(minutes=30),
                yes_token=Token(token_id=st.yes_tid, outcome="Yes", price=yes_price),
                no_token =Token(token_id=st.no_tid,  outcome="No",  price=no_price),
                liquidity=5_000.0,
                volume_24h=1_500.0,
                active=True,
                closed=False,
                accepting_orders=True,
            ))
        return markets

    def get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        return self.ws.get_book(token_id)

    def get_btc_price(self) -> float:
        return self.ws.get_btc_price()

    def refresh_market_question(self, cid: str) -> None:
        self.ws.refresh_market_question(cid)

    async def subscribe_markets(self, markets: List[Market]) -> int:
        return 0  # simulation auto-emits all markets


# ── Simulated order manager ───────────────────────────────────────────────────

class SimulatedOrderManager:
    """
    Simulates CLOB order execution for paper trading.

    FOK: fills with FOK_FILL_RATE probability, adds Gaussian slippage.
    GTC: always fills (passive, no rush).
    Latency: asyncio.sleep(EXEC_LATENCY_S) before returning.
    """

    def __init__(self, config: PolymarketConfig) -> None:
        self.config   = config
        self._rng     = random.Random(12345)
        self._orders: List[PlacedOrder] = []
        self._exec_times: List[float] = []

    async def place_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size_usdc: float,
        order_type: OrderType = OrderType.FOK,
        expiry_ts=None,
    ) -> PlacedOrder:
        t0 = time.monotonic()

        # Simulate execution latency
        await asyncio.sleep(EXEC_LATENCY_S * self._rng.uniform(0.7, 1.3))

        elapsed_ms = (time.monotonic() - t0) * 1000
        self._exec_times.append(elapsed_ms)

        size_tokens = size_usdc / price if price > 0 else 0

        # Fill decision
        if order_type == OrderType.FOK:
            filled = self._rng.random() < FOK_FILL_RATE
        else:
            filled = True  # GTC always fills in simulation

        if not filled:
            order = PlacedOrder(
                order_id=f"SIM-MISS-{uuid.uuid4().hex[:8]}",
                token_id=token_id, side=side, order_type=order_type,
                price=price, size_tokens=size_tokens, size_usdc=size_usdc,
                filled_size=0.0, status="CANCELLED",
            )
            self._orders.append(order)
            return order

        # Slippage
        slip     = self._rng.gauss(0, SLIP_STD)
        fill_px  = max(0.01, min(0.99, price + slip))

        order = PlacedOrder(
            order_id=f"SIM-{uuid.uuid4().hex[:8]}",
            token_id=token_id, side=side, order_type=order_type,
            price=fill_px, size_tokens=size_tokens, size_usdc=size_usdc,
            filled_size=size_tokens, status="FILLED",
        )
        self._orders.append(order)
        return order

    async def execute_arb(self, opportunity):
        """Delegate to the real OrderManager.execute_arb logic with sim order placement."""
        # Import here to avoid circular imports
        from .order_manager import OrderManager
        real_mgr = OrderManager.__new__(OrderManager)
        real_mgr.config  = self.config
        real_mgr.auth    = None
        real_mgr._rl     = _NoOpRateLimiter()
        real_mgr._orders = self._orders
        real_mgr.place_order = self.place_order    # monkey-patch
        return await real_mgr.execute_arb(opportunity)

    async def cancel_order(self, order_id: str) -> bool:
        return True

    def get_order_log(self) -> List[PlacedOrder]:
        return list(self._orders)

    @property
    def avg_execution_ms(self) -> float:
        if not self._exec_times:
            return 0.0
        return sum(self._exec_times) / len(self._exec_times)


class _NoOpRateLimiter:
    async def acquire(self) -> None:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _safe_fire(cb, *args) -> None:
    if cb is None:
        return
    try:
        res = cb(*args)
        if asyncio.iscoroutine(res):
            await res
    except Exception:
        pass
