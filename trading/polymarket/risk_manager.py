"""
risk_manager.py — Section 3, Component 4: Risk Manager (limits & stop-loss).

Implements all Section 5 risk controls:

Position limits
───────────────
• Max 5% per market position  (max_position_pct)
• Max 10% portfolio exposure  (max_portfolio_exposure_pct)

Loss controls
─────────────
• Daily loss caps   — halt if daily P&L < −5% of starting capital
• Kill switch       — manual STOP_ALL command
• Circuit breakers  — auto-halt after N consecutive losing trades;
                      resume after cooldown_s (default 5 min)

Guard logic
───────────
The RiskManager is the gatekeeper before every trade.
execute_arb() in the bot calls risk_manager.approve(opportunity, portfolio)
before OrderManager.execute_arb().

If trading is halted, approve() returns False and logs the reason.
The kill switch can be reset with reset_kill_switch(); the circuit breaker
resets automatically after its cooldown period.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from .config import PolymarketConfig
from .models import (
    ArbExecution,
    ArbOpportunity,
    ArbResult,
    CircuitBreakerState,
    PortfolioState,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Section 3, Component 4: Risk Manager.

    Usage
    -----
    ok = risk.approve(opportunity, portfolio)
    if ok:
        execution = await order_manager.execute_arb(opportunity)
        risk.record_execution(execution, portfolio)
    """

    def __init__(self, config: PolymarketConfig) -> None:
        self.config = config
        self.state  = CircuitBreakerState()
        self._session_start_equity: Optional[float] = None  # set on first approve()
        self._daily_start_equity: Optional[float]   = None
        self._daily_start_date: Optional[datetime]  = None

    # ── Main gate ─────────────────────────────────────────────────────────────

    def approve(
        self,
        opportunity: ArbOpportunity,
        portfolio: PortfolioState,
    ) -> bool:
        """
        Return True if the opportunity is approved for execution.
        Logs the reason for any rejection.
        """
        equity = portfolio.equity

        # Initialise session tracking on first call
        if self._session_start_equity is None:
            self._session_start_equity = equity
        self._init_daily(equity)

        # ── Kill switch (manual) ──────────────────────────────────────────────
        if self.state.kill_switch_active:
            logger.warning(f"KILL SWITCH active: {self.state.kill_reason}")
            return False

        # ── Circuit breaker (automatic) ───────────────────────────────────────
        if self.state.circuit_breaker_active:
            if not self._try_reset_circuit_breaker():
                remaining = self._circuit_breaker_remaining_s()
                logger.warning(
                    f"CIRCUIT BREAKER active  "
                    f"(resets in {remaining:.0f}s, "
                    f"{self.state.consecutive_losses} consecutive losses)"
                )
                return False

        # ── Daily loss cap ────────────────────────────────────────────────────
        if not self._check_daily_loss_cap(equity):
            return False

        # ── Per-market position limit (5%) ────────────────────────────────────
        max_pos_usd = equity * self.config.max_position_pct
        for leg in opportunity.legs:
            if leg.size_usdc > max_pos_usd:
                logger.warning(
                    f"Risk: leg size ${leg.size_usdc:.2f} > "
                    f"max per-market ${max_pos_usd:.2f} (5%)"
                )
                return False

        # ── Portfolio exposure limit (10%) ────────────────────────────────────
        projected = portfolio.total_cost_basis + opportunity.total_cost_usdc
        max_exp   = equity * self.config.max_portfolio_exposure_pct
        if projected > max_exp:
            logger.warning(
                f"Risk: projected exposure ${projected:.2f} > "
                f"max ${max_exp:.2f} (10%)"
            )
            return False

        # ── Sufficient cash ────────────────────────────────────────────────────
        if opportunity.total_cost_usdc > portfolio.cash_usdc:
            logger.debug(
                f"Risk: insufficient cash "
                f"${portfolio.cash_usdc:.2f} < ${opportunity.total_cost_usdc:.2f}"
            )
            return False

        # ── Already in this market ─────────────────────────────────────────────
        if portfolio.has_position(opportunity.condition_id):
            logger.debug(f"Risk: already in market {opportunity.condition_id[:8]}")
            return False

        return True

    # ── Execution feedback ─────────────────────────────────────────────────────

    def record_execution(
        self,
        execution: ArbExecution,
        portfolio: PortfolioState,
    ) -> None:
        """
        Update circuit-breaker state based on the execution result.
        Called after every arb attempt (win or loss).
        """
        equity = portfolio.equity

        # Update daily P&L tracking
        self.state.daily_pnl = equity - (self._daily_start_equity or equity)

        if execution.result == ArbResult.SUCCESS:
            # Winning trade — reset consecutive loss counter
            self.state.consecutive_losses = 0
            logger.debug(
                f"Risk: consecutive_losses reset to 0  "
                f"(daily_pnl=${self.state.daily_pnl:+.2f})"
            )

        elif execution.result in (ArbResult.FAIL_UNWIND, ArbResult.PARTIAL_UNWIND):
            # Actual loss (partial unwind = we lost money)
            self.state.consecutive_losses += 1
            logger.warning(
                f"Risk: consecutive_losses = {self.state.consecutive_losses}  "
                f"(result={execution.result.value})"
            )
            self._maybe_trip_circuit_breaker()

        # MISS and SKIPPED don't count as losses (no money was at risk)

    # ── Kill switch ────────────────────────────────────────────────────────────

    def activate_kill_switch(self, reason: str = "manual") -> None:
        """Section 5: Kill switch — immediately halt all new trading."""
        self.state.kill_switch_active = True
        self.state.kill_reason = reason
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")

    def reset_kill_switch(self) -> None:
        """Re-enable trading after the kill switch was triggered."""
        self.state.kill_switch_active = False
        self.state.kill_reason = ""
        logger.info("Kill switch deactivated — trading resumed")

    # ── Circuit breaker ────────────────────────────────────────────────────────

    def _maybe_trip_circuit_breaker(self) -> None:
        """Trip the circuit breaker if consecutive losses exceed the threshold."""
        threshold = self.config.circuit_breaker_consecutive_losses
        if self.state.consecutive_losses >= threshold:
            self.state.circuit_breaker_active = True
            self.state.circuit_reset_at = (
                datetime.utcnow()
                + timedelta(seconds=self.config.circuit_breaker_cooldown_s)
            )
            logger.critical(
                f"CIRCUIT BREAKER TRIPPED: {self.state.consecutive_losses} "
                f"consecutive losses.  "
                f"Resuming after {self.config.circuit_breaker_cooldown_s}s cooldown."
            )

    def _try_reset_circuit_breaker(self) -> bool:
        """Auto-reset if the cooldown has elapsed."""
        if self.state.circuit_reset_at is None:
            return True
        if datetime.utcnow() >= self.state.circuit_reset_at:
            self.state.circuit_breaker_active = False
            self.state.consecutive_losses = 0
            self.state.circuit_reset_at = None
            logger.info("Circuit breaker reset — trading resumed")
            return True
        return False

    def _circuit_breaker_remaining_s(self) -> float:
        if self.state.circuit_reset_at is None:
            return 0.0
        delta = self.state.circuit_reset_at - datetime.utcnow()
        return max(0.0, delta.total_seconds())

    # ── Daily loss cap ─────────────────────────────────────────────────────────

    def _init_daily(self, equity: float) -> None:
        """Reset daily tracking at the start of a new calendar day."""
        today = datetime.utcnow().date()
        if self._daily_start_date != today:
            self._daily_start_equity = equity
            self._daily_start_date = today
            self.state.daily_pnl = 0.0
            logger.info(f"Daily tracker reset: start_equity=${equity:.2f}")

    def _check_daily_loss_cap(self, equity: float) -> bool:
        """Return False and log if the daily loss cap is breached."""
        if self._daily_start_equity is None:
            return True
        daily_loss = equity - self._daily_start_equity
        cap = self._daily_start_equity * self.config.daily_loss_cap_pct
        if daily_loss < -cap:
            logger.critical(
                f"DAILY LOSS CAP BREACHED: "
                f"${daily_loss:.2f} < −${cap:.2f} "
                f"({self.config.daily_loss_cap_pct*100:.0f}% of ${self._daily_start_equity:.2f}).  "
                f"Trading halted for the day."
            )
            self.activate_kill_switch(
                f"daily loss cap: ${daily_loss:.2f} < −${cap:.2f}"
            )
            return False
        return True

    # ── Status report ──────────────────────────────────────────────────────────

    def status(self) -> str:
        parts = []
        if self.state.trading_halted:
            if self.state.kill_switch_active:
                parts.append(f"KILL_SWITCH({self.state.kill_reason})")
            if self.state.circuit_breaker_active:
                remaining = self._circuit_breaker_remaining_s()
                parts.append(f"CIRCUIT_BREAKER(resets in {remaining:.0f}s)")
        else:
            parts.append("TRADING_OK")

        parts.append(f"consecutive_losses={self.state.consecutive_losses}")
        parts.append(f"daily_pnl=${self.state.daily_pnl:+.2f}")
        return "  ".join(parts)
