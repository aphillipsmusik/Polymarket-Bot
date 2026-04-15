"""
config.py — Polymarket bot configuration (rebuilt per infrastructure guide).

Parameter sources
-----------------
Section 1: Account Setup   → chain_id, sig_type, rate limits
Section 3: Bot Architecture → API endpoints, WebSocket ping intervals
Section 4: Trading Strategies → spread thresholds, fee rates
Section 5: Execution & Risk  → order types, position limits, circuit breakers
Section 6: Performance Metrics → execution success target, response time target
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List


@dataclass
class PolymarketConfig:
    """
    All runtime parameters for the Polymarket arbitrage bot.
    Defaults tuned for $50 USDC starting capital.
    """

    # ── Section 1: Account Setup ──────────────────────────────────────────────
    chain_id: int = 137             # Polygon mainnet
    # Signature type: 0 = MetaMask/EOA, 1 = Magic Link, 2 = Gnosis Safe
    sig_type: int = 0

    # ── Capital ───────────────────────────────────────────────────────────────
    initial_capital: float = 50.0   # Starting USDC

    # ── Section 5: Risk Controls ──────────────────────────────────────────────
    # Per-market position limit (Section 5: "Max 5% per market position")
    max_position_pct: float = 0.05
    # Total portfolio exposure — 20% of capital deployed across all open positions
    max_portfolio_exposure_pct: float = 0.20
    # Maximum single bet size in USDC (absolute cap)
    max_bet_usd: float = 5.0

    # Daily loss cap: halt trading if daily P&L drops below this % of capital
    daily_loss_cap_pct: float = 0.05          # 5%
    # Circuit breaker: halt after N consecutive losing trades
    circuit_breaker_consecutive_losses: int = 5
    # Circuit breaker cool-down before resuming
    circuit_breaker_cooldown_s: int = 300     # 5 minutes

    # ── Section 4: Trading Strategies ─────────────────────────────────────────
    # Sum-to-One Arbitrage
    # Need spread > taker_fee×2 to profit.
    # Guide: "need 3%+ spread to cover 2% fee"
    s2o_min_spread_pct: float = 0.03         # Minimum 3% spread
    s2o_min_profit_usd: float = 0.05         # At least $0.05 profit after fees

    # Endgame Arbitrage (near-certain outcomes 95–99%)
    endgame_min_prob: float = 0.95
    endgame_max_prob: float = 0.99
    endgame_min_roi: float = 0.005           # Min 0.5% ROI

    # Fee model (Section 1 header: "2% platform fee + $0.007 gas per transaction")
    taker_fee_rate: float = 0.02             # 2% per taker trade
    maker_fee_rate: float = 0.00             # 0% maker fee
    gas_cost_usd: float = 0.007             # $0.007 per on-chain tx

    # Market quality filters
    min_liquidity_usd: float = 500.0         # Min total order-book depth
    min_volume_24h_usd: float = 100.0        # Min 24h volume
    max_spread_for_endgame: float = 0.03     # 3-cent max spread on endgame plays

    # Endgame: minimum annualised ROI (filters out slow markets tying up capital)
    # e.g. 0.20 = 20% APY; a 97% market resolving in 60 days yields ~3%/60d ≈ 18% APY → rejected
    endgame_min_annualized_roi: float = 0.20

    # Kelly-proportional position sizing for Sum-to-One arb
    # Bet scales linearly with gross spread; full max_bet is reached at
    # kelly_fraction × s2o_min_spread_pct.  E.g. kelly_fraction=4, min_spread=3%
    # → full size at 12% spread, quarter size at 3% spread.
    kelly_fraction: float = 4.0

    # Order-book freshness: reject cached WS books older than this many seconds
    # and fall back to a live REST fetch instead.
    book_max_age_s: float = 10.0

    # Deduplication: minimum seconds between execution attempts on the same market
    attempt_cooldown_s: float = 30.0

    # ── Section 3: WebSocket (Data Collector) ─────────────────────────────────
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    # Section 3 bottom note: "WebSocket ping intervals: 10s (CLOB), 5s (Real-Time Stream)"
    clob_ws_ping_s: int = 10
    realtime_ws_ping_s: int = 5
    ws_reconnect_delay_s: float = 5.0

    # ── Section 1: Rate Limits ────────────────────────────────────────────────
    # "Rate limits: 100 requests/min (public), 60 orders/min (trading)"
    public_rate_limit_per_min: int = 100
    trading_rate_limit_per_min: int = 60

    # ── Section 5: Order Types ────────────────────────────────────────────────
    # FOK = Fill-or-Kill (arb execution, atomic)
    # GTC = Good-Till-Cancelled (passive maker)
    # GTD = Good-Till-Date (time-boxed)
    default_arb_order_type: str = "FOK"

    # ── REST APIs (Section 3: Bot Architecture) ───────────────────────────────
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"

    # ── Section 6: Performance Targets ───────────────────────────────────────
    # "Execution success: 99.2% target"
    target_execution_success_rate: float = 0.992
    # "Response time: ~80ms target"
    target_response_time_ms: float = 80.0

    # ── Bot Behavior ──────────────────────────────────────────────────────────
    # Arb requires speed — scan every 10 s (not 5 min)
    scan_interval_s: int = 10
    position_check_interval_s: int = 30
    dry_run: bool = True          # Paper trading by default — ALWAYS start here
    log_level: str = "INFO"
    max_api_retries: int = 3
    api_retry_delay_s: float = 1.0
