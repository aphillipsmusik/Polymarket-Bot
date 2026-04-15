"""
trading/polymarket — Polymarket automated arbitrage bot.

Rebuilt per the Polymarket Automated Trading Setup: Complete Infrastructure Guide.

Architecture (Section 3)
─────────────────────────
  Component 1: DataCollector   (WebSocket/REST)  — data_collector.py
  Component 2: StrategyEngine  (signal generation) — strategy_engine.py
  Component 3: OrderManager    (CLOB API)         — order_manager.py
  Component 4: RiskManager     (limits & stop-loss) — risk_manager.py

Strategies (Section 4)
────────────────────────
  Sum-to-One Arbitrage     buy YES + NO when sum < $1.00 − fees
  Combinatorial Arbitrage  same for 3+ outcome markets
  Endgame Arbitrage        95–99% near-certain positions
  Cross-Platform Arbitrage price gap detection vs other platforms

Authentication (Section 1)
────────────────────────────
  L1: Private key signing (EIP-712) via py_clob_client
  L2: HMAC-SHA256 API credentials                    via auth.py

Risk Controls (Section 5)
──────────────────────────
  Max 5% per market position   max_position_pct
  Max 10% portfolio exposure   max_portfolio_exposure_pct
  Daily loss cap               daily_loss_cap_pct
  Kill switch                  risk_manager.activate_kill_switch()
  Circuit breaker              auto-trips after N consecutive losses

Performance Targets (Section 6)
─────────────────────────────────
  Execution success rate : 99.2%
  Response time target   : ~80 ms
"""

from .config import PolymarketConfig
from .models import (
    ArbOpportunity, ArbResult, ArbType,
    Market, OrderBookSnapshot, PortfolioState,
)
from .bot import PolymarketBot

__all__ = [
    "PolymarketConfig",
    "PolymarketBot",
    "ArbOpportunity",
    "ArbResult",
    "ArbType",
    "Market",
    "OrderBookSnapshot",
    "PortfolioState",
]
