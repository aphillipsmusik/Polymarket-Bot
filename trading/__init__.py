"""
Trading Algorithm Module
------------------------
A risk-managed, multi-strategy algorithmic trading system.

DISCLAIMER: No trading algorithm can guarantee profits or "never lose."
All trading involves risk. This system is designed to minimize losses
through disciplined risk management, not eliminate them.

Modules:
    data        — OHLCV data interface and synthetic data generator
    indicators  — Technical indicators (SMA, EMA, RSI, Bollinger Bands, ATR, MACD)
    signals     — Signal generators: momentum, mean-reversion, trend-following
    risk        — Position sizing, stop-loss, drawdown controls
    portfolio   — Portfolio state and order management
    backtest    — Event-driven backtesting engine
    metrics     — Performance metrics (Sharpe, Sortino, max drawdown, win rate)
    algorithm   — Top-level algorithm that wires everything together
"""
