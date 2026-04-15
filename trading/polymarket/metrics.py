"""
metrics.py — Section 6: Performance Metrics.

Tracks all Section 6 metrics in real time:
  • Profit (realized / unrealized)
  • Fill rates
  • Slippage
  • Win rate
  • Sharpe ratio
  • Execution success: 99.2% target
  • Response time: ~80ms target

The MetricsTracker is updated after every arb execution and prints a
formatted dashboard on demand.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import List, Optional

from .config import PolymarketConfig
from .models import ArbExecution, ArbResult, ClosedPosition, PerformanceSnapshot

logger = logging.getLogger(__name__)


class MetricsTracker:
    """
    Section 6: Performance Metrics.

    Aggregates execution data and computes summary statistics.
    """

    def __init__(self, config: PolymarketConfig) -> None:
        self.config  = config
        self._snap   = PerformanceSnapshot()
        self._equity_curve: List[float] = []
        self._daily_returns: List[float] = []
        self._last_equity: Optional[float] = None

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_arb_execution(self, execution: ArbExecution) -> None:
        """Called after every arb attempt."""
        snap = self._snap
        snap.total_trades += 1
        snap.response_times_ms.append(execution.execution_time_ms)

        if execution.result == ArbResult.SUCCESS:
            snap.successful_executions += 1
            snap.realized_pnl += execution.realized_profit_usd
        elif execution.result == ArbResult.MISS:
            snap.missed_opportunities += 1
        else:
            snap.failed_executions += 1

        snap.total_slippage  += execution.slippage_usd
        snap.total_fees_paid += execution.opportunity.fee_cost_usdc
        snap.total_gas_paid  += execution.opportunity.gas_cost_usdc

        # Recalculate derived stats
        if snap.total_trades > 0:
            snap.avg_slippage_pct = (
                snap.total_slippage / snap.total_trades
            )

        if snap.successful_executions + snap.failed_executions > 0:
            denom = snap.successful_executions + snap.failed_executions
            snap.win_rate = snap.successful_executions / denom

        # Warn if execution success rate drops below target
        if snap.total_trades >= 10:
            if snap.execution_success_rate < self.config.target_execution_success_rate:
                logger.warning(
                    f"Execution success rate {snap.execution_success_rate*100:.1f}% "
                    f"below target {self.config.target_execution_success_rate*100:.1f}%"
                )

        # Warn on slow executions
        if execution.execution_time_ms > self.config.target_response_time_ms * 3:
            logger.warning(
                f"Slow execution: {execution.execution_time_ms:.0f}ms "
                f"(target ~{self.config.target_response_time_ms:.0f}ms)"
            )

    def record_closed_position(self, closed: ClosedPosition) -> None:
        self._snap.realized_pnl += closed.pnl
        self._snap.total_fees_paid += 0.0  # fees already counted in arb execution

    def record_equity(self, equity: float) -> None:
        """Snapshot equity to drive Sharpe calculation."""
        self._equity_curve.append(equity)

        if self._last_equity is not None and self._last_equity > 0:
            ret = (equity - self._last_equity) / self._last_equity
            self._daily_returns.append(ret)

            # Recompute Sharpe when we have enough data
            if len(self._daily_returns) >= 10:
                self._snap.sharpe_ratio = _sharpe(self._daily_returns)

        self._last_equity = equity

    # ── Reporting ─────────────────────────────────────────────────────────────

    def print_dashboard(self, current_equity: float, initial_capital: float) -> None:
        """Print a formatted Section 6 dashboard to the logger."""
        snap = self._snap
        pnl  = current_equity - initial_capital

        # Execution success rate vs target
        success_pct = snap.execution_success_rate * 100
        target_pct  = self.config.target_execution_success_rate * 100
        success_flag = "OK" if success_pct >= target_pct else "BELOW TARGET"

        # Response time vs target
        avg_rt   = snap.avg_response_time_ms
        p99_rt   = snap.p99_response_time_ms
        rt_target = self.config.target_response_time_ms
        rt_flag  = "OK" if avg_rt <= rt_target else "ABOVE TARGET"

        sep = "─" * 62
        logger.info(
            f"\n  {sep}"
            f"\n  PERFORMANCE METRICS  (Section 6)"
            f"\n  {sep}"
            f"\n  Profit (realized)   : ${snap.realized_pnl:+.4f}"
            f"\n  Equity change       : ${pnl:+.4f}"
            f"\n  Total fees paid     : ${snap.total_fees_paid:.4f}"
            f"\n  Total gas paid      : ${snap.total_gas_paid:.4f}"
            f"\n  Total slippage      : ${snap.total_slippage:.4f}"
            f"\n  {sep}"
            f"\n  Total arb attempts  : {snap.total_trades}"
            f"\n    Successful        : {snap.successful_executions}"
            f"\n    Failed / unwind   : {snap.failed_executions}"
            f"\n    Missed (no fill)  : {snap.missed_opportunities}"
            f"\n  Win rate            : {snap.win_rate*100:.1f}%"
            f"\n  Fill rate (success) : {success_pct:.1f}%  [{success_flag}]"
            f"\n    Target            : {target_pct:.1f}%"
            f"\n  Sharpe ratio        : {snap.sharpe_ratio:.3f}"
            f"\n  {sep}"
            f"\n  Response time (avg) : {avg_rt:.1f}ms  [{rt_flag}]"
            f"\n  Response time (p99) : {p99_rt:.1f}ms"
            f"\n    Target            : ~{rt_target:.0f}ms"
            f"\n  {sep}"
        )

    @property
    def snapshot(self) -> PerformanceSnapshot:
        return self._snap


# ── Statistical helpers ───────────────────────────────────────────────────────

def _sharpe(returns: List[float], risk_free_rate: float = 0.0) -> float:
    """Compute annualised Sharpe ratio from a list of periodic returns."""
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0.0:
        return 0.0
    excess = mean - risk_free_rate
    # Annualise assuming daily data (252 trading days)
    return (excess / std) * math.sqrt(252)
