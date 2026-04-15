"""
models.py — Data models rebuilt per infrastructure guide.

New additions vs previous version
-----------------------------------
• ArbOpportunity  — represents a detected arbitrage opportunity with legs
• ArbLeg          — one side of a multi-leg arb trade
• OrderType       — FOK / GTC / GTD  (Section 5)
• ArbResult       — outcome of an arb execution attempt
• PerformanceSnapshot — Section 6 metrics
• CircuitBreakerState — Section 5 kill-switch state
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


# ── Enums ─────────────────────────────────────────────────────────────────────

class OrderType(Enum):
    """Section 5: Execution — order types."""
    FOK = "FOK"   # Fill-or-Kill: all or nothing, immediate
    GTC = "GTC"   # Good-Till-Cancelled: rests in book until filled or cancelled
    GTD = "GTD"   # Good-Till-Date: expires at a specified timestamp


class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"


class ArbType(Enum):
    """Section 4: Trading Strategies."""
    SUM_TO_ONE    = "sum_to_one"     # YES + NO both < $1
    COMBINATORIAL = "combinatorial"  # multi-outcome prices sum < $1
    ENDGAME       = "endgame"        # 95-99% near-certain outcome
    CROSS_PLATFORM = "cross_platform" # price gap between platforms


class ArbResult(Enum):
    """Outcome of an arb execution attempt."""
    SUCCESS        = "success"         # both legs filled, profit locked
    MISS           = "miss"            # first leg not filled (no exposure)
    PARTIAL_UNWIND = "partial_unwind"  # first leg filled, second failed; unwound
    FAIL_UNWIND    = "fail_unwind"     # partial fill, unwind also failed (bad)
    SKIPPED        = "skipped"         # risk manager blocked the trade


# ── Tokens & Markets ──────────────────────────────────────────────────────────

@dataclass
class Token:
    token_id: str
    outcome: str       # "Yes" / "No" / outcome label
    price: float       # Implied probability


@dataclass
class Market:
    condition_id: str
    question: str
    description: str
    end_date: Optional[datetime]
    yes_token: Token
    no_token: Token
    liquidity: float
    volume_24h: float
    active: bool
    closed: bool
    accepting_orders: bool

    @property
    def days_to_resolution(self) -> Optional[float]:
        if self.end_date is None:
            return None
        delta = self.end_date.replace(tzinfo=None) - datetime.utcnow()
        return max(0.0, delta.total_seconds() / 86_400)

    @property
    def is_tradeable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders

    def __str__(self) -> str:
        end = self.end_date.strftime("%Y-%m-%d") if self.end_date else "?"
        return (
            f"[{self.condition_id[:8]}] {self.question[:55]}  "
            f"YES={self.yes_token.price:.3f}  NO={self.no_token.price:.3f}  "
            f"liq=${self.liquidity:,.0f}  ends={end}"
        )


# ── Order Book ────────────────────────────────────────────────────────────────

@dataclass
class OrderLevel:
    price: float
    size: float      # USDC


@dataclass
class OrderBookSnapshot:
    token_id: str
    bids: List[OrderLevel]   # highest first
    asks: List[OrderLevel]   # lowest first
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 6)

    def depth_at_price(self, price: float, side: str, tolerance: float = 0.005) -> float:
        """Available liquidity within `tolerance` of a given price."""
        levels = self.bids if side.upper() == "BID" else self.asks
        return sum(
            l.size for l in levels
            if abs(l.price - price) <= tolerance
        )


# ── Arbitrage Models ──────────────────────────────────────────────────────────

@dataclass
class ArbLeg:
    """One side of a multi-leg arbitrage trade."""
    token_id: str
    outcome_label: str    # "YES" or "NO"
    side: OrderSide
    price: float          # limit price to use
    size_tokens: float    # number of tokens
    size_usdc: float      # USDC cost = price × size_tokens
    order_type: OrderType = OrderType.FOK


@dataclass
class ArbOpportunity:
    """
    A detected arbitrage opportunity.

    Sum-to-One example (Section 4):
      YES ask = 0.48, NO ask = 0.50 → sum = 0.98 < 1.00
      Buy both → pay $0.98, receive $1.00 at resolution → 2% gross profit
      After fees (2% taker × 2): net ≈ (spread - 2×fee%) × size

    Profit calculation:
      gross_profit_pct = 1.0 - (yes_ask + no_ask)
      fee_cost_pct     = taker_fee × (yes_ask + no_ask) × 2  (both legs taker)
      net_profit_pct   = gross_profit_pct - fee_cost_pct - gas/size
    """
    arb_type: ArbType
    condition_id: str
    question: str
    legs: List[ArbLeg]

    gross_profit_pct: float   # before fees
    net_profit_usd: float     # after fees and gas, in USDC
    net_profit_pct: float     # net_profit_usd / total_cost
    total_cost_usdc: float    # sum of all leg costs
    fee_cost_usdc: float      # total platform fees
    gas_cost_usdc: float      # total gas

    detected_at: datetime = field(default_factory=datetime.utcnow)

    def __str__(self) -> str:
        legs_str = "  +  ".join(
            f"{l.outcome_label}@{l.price:.4f}(${l.size_usdc:.2f})"
            for l in self.legs
        )
        return (
            f"{self.arb_type.value:14s}  {self.question[:50]:50s}  "
            f"{legs_str}  "
            f"net=${self.net_profit_usd:+.4f} ({self.net_profit_pct*100:+.2f}%)"
        )


@dataclass
class ArbExecution:
    """Records the outcome of executing an arb opportunity."""
    opportunity: ArbOpportunity
    result: ArbResult
    filled_legs: List[ArbLeg]
    realized_profit_usd: float
    slippage_usd: float
    execution_time_ms: float
    executed_at: datetime = field(default_factory=datetime.utcnow)


# ── Order Lifecycle ───────────────────────────────────────────────────────────

@dataclass
class PlacedOrder:
    order_id: str
    token_id: str
    side: OrderSide
    order_type: OrderType
    price: float
    size_tokens: float
    size_usdc: float
    placed_at: datetime = field(default_factory=datetime.utcnow)
    filled_size: float = 0.0
    status: str = "PENDING"    # PENDING | FILLED | PARTIAL | CANCELLED | EXPIRED


# ── Positions ─────────────────────────────────────────────────────────────────

@dataclass
class OpenPosition:
    condition_id: str
    question: str
    token_id: str
    direction: str             # "YES" or "NO"
    quantity: float
    entry_price: float
    cost_basis: float
    entry_time: datetime
    arb_type: ArbType
    # Set when we're waiting for the complementary leg to complete
    is_unhedged: bool = False

    def current_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return self.current_value(price) - self.cost_basis

    def __str__(self) -> str:
        flag = " [UNHEDGED!]" if self.is_unhedged else ""
        return (
            f"{self.direction:3s}  qty={self.quantity:.4f}  "
            f"cost=${self.cost_basis:.2f}  entry={self.entry_price:.4f}  "
            f"{self.question[:45]}{flag}"
        )


@dataclass
class ClosedPosition:
    condition_id: str
    question: str
    token_id: str
    direction: str
    quantity: float
    entry_price: float
    exit_price: float
    cost_basis: float
    exit_proceeds: float
    pnl: float
    pnl_pct: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    arb_type: ArbType

    def __str__(self) -> str:
        sign = "+" if self.pnl >= 0 else ""
        return (
            f"{self.direction:3s}  {self.exit_reason:18s}  "
            f"pnl={sign}${self.pnl:.4f}({sign}{self.pnl_pct*100:.2f}%)  "
            f"{self.question[:45]}"
        )


# ── Portfolio ─────────────────────────────────────────────────────────────────

@dataclass
class PortfolioState:
    cash_usdc: float
    open_positions: List[OpenPosition] = field(default_factory=list)
    closed_positions: List[ClosedPosition] = field(default_factory=list)
    arb_executions: List[ArbExecution] = field(default_factory=list)

    @property
    def total_cost_basis(self) -> float:
        return sum(p.cost_basis for p in self.open_positions)

    @property
    def equity(self) -> float:
        return self.cash_usdc + self.total_cost_basis

    @property
    def total_realized_pnl(self) -> float:
        return sum(p.pnl for p in self.closed_positions)

    def has_position(self, condition_id: str) -> bool:
        return any(p.condition_id == condition_id for p in self.open_positions)

    def get_position(self, condition_id: str) -> Optional[OpenPosition]:
        return next(
            (p for p in self.open_positions if p.condition_id == condition_id),
            None,
        )


# ── Performance Metrics (Section 6) ──────────────────────────────────────────

@dataclass
class PerformanceSnapshot:
    """Section 6: Performance Metrics tracking."""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    total_trades: int = 0
    successful_executions: int = 0
    failed_executions: int = 0
    missed_opportunities: int = 0

    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    total_fees_paid: float = 0.0
    total_gas_paid: float = 0.0
    total_slippage: float = 0.0

    avg_fill_rate: float = 0.0          # fraction of order filled
    avg_slippage_pct: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0

    response_times_ms: List[float] = field(default_factory=list)

    @property
    def execution_success_rate(self) -> float:
        if self.total_trades == 0:
            return 1.0
        return self.successful_executions / self.total_trades

    @property
    def avg_response_time_ms(self) -> float:
        if not self.response_times_ms:
            return 0.0
        return sum(self.response_times_ms) / len(self.response_times_ms)

    @property
    def p99_response_time_ms(self) -> float:
        if not self.response_times_ms:
            return 0.0
        sorted_times = sorted(self.response_times_ms)
        idx = int(len(sorted_times) * 0.99)
        return sorted_times[min(idx, len(sorted_times) - 1)]


# ── Circuit Breaker ───────────────────────────────────────────────────────────

@dataclass
class CircuitBreakerState:
    """Section 5: Kill switches and circuit breakers."""
    kill_switch_active: bool = False         # manual emergency stop
    circuit_breaker_active: bool = False     # automatic stop on loss streak
    consecutive_losses: int = 0
    daily_pnl: float = 0.0
    circuit_reset_at: Optional[datetime] = None
    kill_reason: str = ""

    @property
    def trading_halted(self) -> bool:
        return self.kill_switch_active or self.circuit_breaker_active
