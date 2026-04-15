"""
strategy_engine.py — Section 3, Component 2: Strategy Engine (signal generation).

Implements all four Section 4 arbitrage strategies:

1. Sum-to-One Arbitrage  (primary)
   ─────────────────────────────────────────────────────────────────────────
   Condition:  ask_yes + ask_no < 1.00 − fee_threshold
   Action:     Buy YES tokens + Buy NO tokens simultaneously (FOK)
   Payout:     Exactly one side resolves to $1/token — guaranteed profit
   Example:    YES $0.48 + NO $0.50 = $0.98 cost → $1.00 payout
               Net after 2% taker × 2 legs: spread − fee_cost > 0

   Profit formula (per K tokens):
     gross  = K × (1 − ask_yes − ask_no)
     fees   = K × (ask_yes + ask_no) × taker_fee × 2     ← both legs taker
     gas    = 2 × gas_cost_usd
     net    = gross − fees − gas
     min_K  = (2 × gas) / (gross_pct − fee_pct) to breakeven on gas

2. Combinatorial Arbitrage
   ─────────────────────────────────────────────────────────────────────────
   Like Sum-to-One but for markets with 3+ mutually-exclusive outcomes.
   If sum(all outcome prices) < 1.00 − fees, buy all outcomes.
   (Simplified: scan sum > 2-leg markets when Polymarket adds them.)

3. Endgame Arbitrage  (secondary)
   ─────────────────────────────────────────────────────────────────────────
   Condition:  YES price in [95%, 99%] — near-certain outcome
   Action:     Buy the near-certain token; hold until resolution
   Rationale:  Market may underprice near-certain events; 1–5% yield
               Risk: event flips at the last second

4. Cross-Platform Arbitrage  (detected only — execution is external)
   ─────────────────────────────────────────────────────────────────────────
   Condition:  Polymarket YES price differs from another platform by ≥ X%
   Action:     Buy cheap, sell expensive (requires external platform access)
   This version only flags the discrepancy — no automated cross-platform orders.

Speed requirement
─────────────────
Section 3 note: "Arbitrage windows last only seconds."
analyze() is called on every WebSocket book update — it must be O(1), no I/O.
All I/O (order book fetches) happens in the DataCollector.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from .config import PolymarketConfig
from .models import (
    ArbLeg,
    ArbOpportunity,
    ArbType,
    Market,
    OrderBookSnapshot,
    OrderSide,
    OrderType,
)

logger = logging.getLogger(__name__)


class StrategyEngine:
    """
    Section 3, Component 2: Strategy Engine.

    analyze_market() is the hot path — called on every WS book update.
    It must return quickly (target < 5 ms) so the bot can react within 80 ms.
    """

    def __init__(self, config: PolymarketConfig) -> None:
        self.config = config

    # ── Main entry: called on every book update ───────────────────────────────

    def analyze_market(
        self,
        market: Market,
        yes_book: Optional[OrderBookSnapshot],
        no_book: Optional[OrderBookSnapshot],
    ) -> Optional[ArbOpportunity]:
        """
        Scan a single market for any arbitrage opportunity.

        Called on every WebSocket order-book update.
        Returns the best opportunity found, or None.

        Priority: Sum-to-One > Endgame
        (Cross-platform and Combinatorial require additional data.)
        """
        if not market.is_tradeable:
            return None

        # 1. Sum-to-One (best risk-adjusted return)
        if yes_book is not None and no_book is not None:
            opp = self._scan_sum_to_one(market, yes_book, no_book)
            if opp is not None:
                return opp

        # 2. Endgame (single-leg, near-certain)
        if yes_book is not None:
            opp = self._scan_endgame(market, yes_book)
            if opp is not None:
                return opp

        return None

    # ── Strategy 1: Sum-to-One Arbitrage ─────────────────────────────────────

    def _scan_sum_to_one(
        self,
        market: Market,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
    ) -> Optional[ArbOpportunity]:
        """
        Section 4: Sum-to-One Arbitrage.

        Buy K tokens of YES at ask_yes AND K tokens of NO at ask_no.
        At resolution exactly one side pays out K USDC → guaranteed profit
        if (ask_yes + ask_no) is sufficiently below 1.00.

        Fee model (both legs are taker FOK orders):
          fee_per_leg  = taker_fee × size_usdc
          total_fee    = taker_fee × (ask_yes × K + ask_no × K)
                       = taker_fee × sum × K
          two-leg total = taker_fee × sum × K × 2

          net_per_token = (1 − sum) − taker_fee × sum × 2 − gas/(2K)
        """
        ask_yes = yes_book.best_ask
        ask_no  = no_book.best_ask

        if ask_yes is None or ask_no is None:
            return None
        if ask_yes <= 0 or ask_no <= 0:
            return None

        price_sum = ask_yes + ask_no
        gross_spread = 1.0 - price_sum

        # Quick rejection: gross spread must exceed minimum threshold
        if gross_spread < self.config.s2o_min_spread_pct:
            return None

        # Compute optimal bet size K (tokens per leg)
        # Use the smaller of: yes liquidity, no liquidity, max position limit
        yes_liq = yes_book.depth_at_price(ask_yes, "ASK")
        no_liq  = no_book.depth_at_price(ask_no,  "ASK")

        equity_cap = self.config.initial_capital * self.config.max_position_pct
        usdc_per_leg = min(
            yes_liq,
            no_liq,
            equity_cap,
            self.config.max_bet_usd,
        )

        if usdc_per_leg < 1.0:
            return None

        k_yes = usdc_per_leg / ask_yes     # tokens on YES leg
        k_no  = usdc_per_leg / ask_no      # tokens on NO leg
        # For guaranteed equal payout at resolution, use equal token count
        k = min(k_yes, k_no)

        if k < 0.01:
            return None

        # Cost of each leg
        cost_yes  = k * ask_yes
        cost_no   = k * ask_no
        total_cost = cost_yes + cost_no

        # Platform fees (both taker)
        fee_yes = cost_yes * self.config.taker_fee_rate
        fee_no  = cost_no  * self.config.taker_fee_rate
        total_fee = fee_yes + fee_no

        # Gas for 2 transactions
        gas = 2 * self.config.gas_cost_usd

        # Net profit
        gross_profit = k * gross_spread         # = k × (1 − sum)
        net_profit   = gross_profit - total_fee - gas
        net_pct      = net_profit / total_cost if total_cost > 0 else 0

        if net_profit < self.config.s2o_min_profit_usd:
            return None

        legs = [
            ArbLeg(
                token_id=market.yes_token.token_id,
                outcome_label="YES",
                side=OrderSide.BUY,
                price=ask_yes,
                size_tokens=k,
                size_usdc=round(cost_yes, 4),
                order_type=OrderType.FOK,
            ),
            ArbLeg(
                token_id=market.no_token.token_id,
                outcome_label="NO",
                side=OrderSide.BUY,
                price=ask_no,
                size_tokens=k,
                size_usdc=round(cost_no, 4),
                order_type=OrderType.FOK,
            ),
        ]

        return ArbOpportunity(
            arb_type=ArbType.SUM_TO_ONE,
            condition_id=market.condition_id,
            question=market.question,
            legs=legs,
            gross_profit_pct=round(gross_spread, 6),
            net_profit_usd=round(net_profit, 6),
            net_profit_pct=round(net_pct, 6),
            total_cost_usdc=round(total_cost, 4),
            fee_cost_usdc=round(total_fee, 6),
            gas_cost_usdc=round(gas, 4),
        )

    # ── Strategy 2: Combinatorial Arbitrage ──────────────────────────────────

    def scan_combinatorial(
        self,
        market: Market,
        books: Dict[str, OrderBookSnapshot],   # token_id → book
    ) -> Optional[ArbOpportunity]:
        """
        Section 4: Combinatorial Arbitrage.

        For markets with N ≥ 3 mutually exclusive outcomes,
        buy all outcomes if sum(ask_i) < 1.00 − fees.

        Currently Polymarket primarily offers binary (YES/NO) markets, so this
        is called when a market has extra outcome tokens beyond YES/NO.
        For binary markets, this reduces to Sum-to-One.
        """
        asks = []
        legs = []
        total_ask = 0.0

        for token in [market.yes_token, market.no_token]:
            book = books.get(token.token_id)
            if book is None or book.best_ask is None:
                return None
            asks.append(book.best_ask)
            total_ask += book.best_ask

        gross_spread = 1.0 - total_ask
        if gross_spread < self.config.s2o_min_spread_pct:
            return None

        # Same fee/profit logic as sum-to-one
        equity_cap = self.config.initial_capital * self.config.max_position_pct
        usdc_per_leg = min(equity_cap, self.config.max_bet_usd)
        k = usdc_per_leg / max(asks) if max(asks) > 0 else 0

        if k < 0.01:
            return None

        total_cost = sum(k * a for a in asks)
        total_fee  = total_cost * self.config.taker_fee_rate * 2
        gas        = len(asks) * self.config.gas_cost_usd
        net_profit = k * gross_spread - total_fee - gas

        if net_profit < self.config.s2o_min_profit_usd:
            return None

        tokens = [market.yes_token, market.no_token]
        legs = [
            ArbLeg(
                token_id=tok.token_id,
                outcome_label=tok.outcome,
                side=OrderSide.BUY,
                price=ask,
                size_tokens=round(k, 4),
                size_usdc=round(k * ask, 4),
                order_type=OrderType.FOK,
            )
            for tok, ask in zip(tokens, asks)
        ]

        return ArbOpportunity(
            arb_type=ArbType.COMBINATORIAL,
            condition_id=market.condition_id,
            question=market.question,
            legs=legs,
            gross_profit_pct=round(gross_spread, 6),
            net_profit_usd=round(net_profit, 6),
            net_profit_pct=round(net_profit / total_cost, 6) if total_cost else 0,
            total_cost_usdc=round(total_cost, 4),
            fee_cost_usdc=round(total_fee, 6),
            gas_cost_usdc=round(gas, 4),
        )

    # ── Strategy 3: Endgame Arbitrage ─────────────────────────────────────────

    def _scan_endgame(
        self,
        market: Market,
        yes_book: OrderBookSnapshot,
    ) -> Optional[ArbOpportunity]:
        """
        Section 4: Endgame Arbitrage — 95–99% probability positions.

        Buy the near-certain outcome and hold to resolution.
        Profit = (1.00 − ask_price) − taker_fee − gas/size.

        Risk: the market could flip.  Mitigated by staying within 99% max.
        """
        ask_yes = yes_book.best_ask
        if ask_yes is None:
            return None

        # Check if YES is in the endgame zone
        if not (self.config.endgame_min_prob <= ask_yes <= self.config.endgame_max_prob):
            # Maybe NO is near-certain instead (YES at 1–5%)
            ask_no = 1.0 - (yes_book.best_bid or ask_yes)
            if not (self.config.endgame_min_prob <= ask_no <= self.config.endgame_max_prob):
                return None
            # Flip to buy NO side
            return self._endgame_leg(
                market, market.no_token.token_id, "NO", ask_no
            )

        return self._endgame_leg(
            market, market.yes_token.token_id, "YES", ask_yes
        )

    def _endgame_leg(
        self,
        market: Market,
        token_id: str,
        label: str,
        ask: float,
    ) -> Optional[ArbOpportunity]:
        """Build a single-leg endgame opportunity."""
        # Payout is $1.00 at resolution
        gross_profit_per_token = 1.0 - ask
        fee_per_token          = ask * self.config.taker_fee_rate

        equity_cap  = self.config.initial_capital * self.config.max_position_pct
        usdc_size   = min(equity_cap, self.config.max_bet_usd)
        k           = usdc_size / ask

        gross       = k * gross_profit_per_token
        fee         = k * fee_per_token
        gas         = self.config.gas_cost_usd
        net_profit  = gross - fee - gas
        net_pct     = net_profit / usdc_size if usdc_size > 0 else 0

        if net_pct < self.config.endgame_min_roi:
            return None

        leg = ArbLeg(
            token_id=token_id,
            outcome_label=label,
            side=OrderSide.BUY,
            price=ask,
            size_tokens=round(k, 4),
            size_usdc=round(usdc_size, 4),
            order_type=OrderType.GTC,   # GTC for endgame — no rush, but still execute
        )

        return ArbOpportunity(
            arb_type=ArbType.ENDGAME,
            condition_id=market.condition_id,
            question=market.question,
            legs=[leg],
            gross_profit_pct=round(gross_profit_per_token, 6),
            net_profit_usd=round(net_profit, 6),
            net_profit_pct=round(net_pct, 6),
            total_cost_usdc=round(usdc_size, 4),
            fee_cost_usdc=round(fee, 6),
            gas_cost_usdc=round(gas, 4),
        )

    # ── Strategy 4: Cross-Platform Arbitrage (detection only) ─────────────────

    def flag_cross_platform(
        self,
        market: Market,
        polymarket_ask: float,
        external_prob: float,    # probability from another platform
        platform_name: str = "external",
    ) -> Optional[ArbOpportunity]:
        """
        Section 4: Cross-Platform Arbitrage.

        Flag when Polymarket price deviates significantly from another platform.
        This version only DETECTS — actual cross-platform execution requires
        integration with the other platform's API.

        Returns an opportunity with ArbType.CROSS_PLATFORM if the gap is
        large enough to be worth investigating.
        """
        gap = external_prob - polymarket_ask
        if abs(gap) < 0.05:   # Need at least 5% gap to be worth flagging
            return None

        label = "YES" if gap > 0 else "NO"
        leg_price = polymarket_ask if gap > 0 else (1.0 - polymarket_ask)

        equity_cap = self.config.initial_capital * self.config.max_position_pct
        k = min(equity_cap, self.config.max_bet_usd) / leg_price
        cost = k * leg_price
        fee  = cost * self.config.taker_fee_rate
        gas  = self.config.gas_cost_usd
        net  = cost * abs(gap) - fee - gas

        if net <= 0:
            return None

        leg = ArbLeg(
            token_id=(market.yes_token.token_id if label == "YES"
                      else market.no_token.token_id),
            outcome_label=label,
            side=OrderSide.BUY,
            price=round(leg_price, 4),
            size_tokens=round(k, 4),
            size_usdc=round(cost, 4),
            order_type=OrderType.FOK,
        )

        logger.info(
            f"CROSS-PLATFORM FLAG  {market.question[:50]}  "
            f"Polymarket={polymarket_ask:.3f}  {platform_name}={external_prob:.3f}  "
            f"gap={gap:+.3f}  net=${net:.4f}"
        )

        return ArbOpportunity(
            arb_type=ArbType.CROSS_PLATFORM,
            condition_id=market.condition_id,
            question=market.question,
            legs=[leg],
            gross_profit_pct=round(abs(gap), 6),
            net_profit_usd=round(net, 6),
            net_profit_pct=round(net / cost, 6) if cost else 0,
            total_cost_usdc=round(cost, 4),
            fee_cost_usdc=round(fee, 6),
            gas_cost_usdc=round(gas, 4),
        )

    # ── Batch scanner (for initial REST-based pass) ───────────────────────────

    def batch_scan(
        self,
        markets: List[Market],
        get_book,          # callable: token_id → Optional[OrderBookSnapshot]
    ) -> List[ArbOpportunity]:
        """
        Scan a list of markets and return all viable opportunities.
        Sorted by net_profit_usd descending.
        """
        opps: List[ArbOpportunity] = []
        for market in markets:
            if not market.is_tradeable:
                continue
            yes_book = get_book(market.yes_token.token_id)
            no_book  = get_book(market.no_token.token_id)
            opp = self.analyze_market(market, yes_book, no_book)
            if opp is not None:
                opps.append(opp)

        opps.sort(key=lambda o: o.net_profit_usd, reverse=True)
        if opps:
            logger.info(f"Batch scan: {len(opps)} opportunities found")
            for o in opps[:5]:
                logger.info(f"  {o}")
        return opps
