# Polymarket Arbitrage Bot

An automated arbitrage trading bot for [Polymarket](https://polymarket.com) prediction markets on Polygon. It detects price inefficiencies across binary outcome markets and executes multi-leg trades in real time.

> **Risk disclaimer:** Arbitrage windows last only seconds. Only 0.51% of users earn $1,000+. This software is provided for educational purposes. Trade only what you can afford to lose.

---

## How It Works

Polymarket's binary markets always resolve to exactly $1.00 — either YES or NO wins. The bot exploits moments when the sum of prices for all outcomes drops below $1.00 (minus fees), meaning you can buy every outcome for less than the guaranteed payout.

### Architecture

The bot is split into four components that run concurrently:

```
DataCollector → StrategyEngine → RiskManager → OrderManager
     ↑                                               ↓
  WebSocket / REST APIs                       Polymarket CLOB API
```

| Component | File | Role |
|---|---|---|
| **DataCollector** | `trading/polymarket/data_collector.py` | Maintains a real-time order-book cache via WebSocket and Gamma REST API |
| **StrategyEngine** | `trading/polymarket/strategy_engine.py` | Scans cached order books for arbitrage opportunities (~5ms hot path) |
| **RiskManager** | `trading/polymarket/risk_manager.py` | Enforces position limits, daily loss caps, and circuit breakers before any trade |
| **OrderManager** | `trading/polymarket/order_manager.py` | Places FOK/GTC orders via the Polymarket CLOB API; handles leg-2 failures by unwinding |

The `bot.py` orchestrator runs three async tasks in a single event loop:
1. **WebSocket feed** — real-time order-book updates (10s ping)
2. **Scanner** — fetches markets, subscribes to tokens, and runs strategy analysis every 10s
3. **Monitor** — checks open positions for stop conditions every 30s

---

## Arbitrage Strategies

### 1. Sum-to-One (primary)
Buy YES and NO simultaneously. If `ask_yes + ask_no < 1.00 - fees`, one side always pays out $1.00.

```
Example: YES @ $0.48 + NO @ $0.50 = $0.98 cost → $1.00 guaranteed payout
Net profit ≈ $0.02 per token (minus ~2% taker fee + gas)
```

Execution uses Fill-or-Kill (FOK) orders. If leg 1 fills but leg 2 fails, leg 1 is immediately unwound.

### 2. Endgame
Buy a near-certain outcome when YES probability is between 95–99%. Holds to resolution.

### 3. Combinatorial
Extends Sum-to-One to markets with more than two outcomes — buy all outcomes when their total cost is below $1.00.

### 4. Cross-Platform (detection only)
Flags price gaps of 5%+ between Polymarket and other platforms. Manual execution required on the external platform.

---

## Risk Controls

| Control | Default | Description |
|---|---|---|
| Max position per market | 5% of capital | Prevents over-concentration |
| Max total exposure | 10% of capital | Portfolio-level cap |
| Max bet size | $5 | Hard per-trade ceiling |
| Daily loss cap | 5% of starting capital | Halts all trading for the day |
| Circuit breaker | 5 consecutive losses | 5-minute cooldown before resuming |
| Kill switch | Manual (`--kill`) | Emergency stop |

---

## Getting Started

### Requirements

- Python 3.10+
- Polygon wallet with USDC (live mode only)

### Install

```bash
git clone https://github.com/aphillipsmusik/polymarket-bot.git
cd polymarket-bot
pip install -r requirements.txt
```

### Paper Trading (no wallet needed)

Run a fully offline simulation against synthetic markets:

```bash
python simulate.py --scans 30
```

Or open the live web dashboard at `http://localhost:5000`:

```bash
python dashboard.py
```

### Live Trading

1. Copy the environment template and add your private key:

```bash
cp .env.example .env
# Edit .env and set POLYMARKET_PRIVATE_KEY=0x...
```

2. Fund your Polygon wallet with USDC on mainnet.

3. Do a dry-run against real markets first (no orders placed):

```bash
python polymarket_bot.py --scans 5
```

4. Start live trading:

```bash
python polymarket_bot.py --live --capital 50
```

---

## CLI Reference

```
python polymarket_bot.py [OPTIONS]

Mode:
  --live                 Enable live order submission (default: dry-run)

Capital:
  --capital FLOAT        Starting capital in USD (default: 50)
  --max-bet FLOAT        Max size per trade (default: 5)
  --max-position-pct N   Max % of capital per market position (default: 5)
  --max-exposure-pct N   Max % of capital in open positions (default: 10)

Strategy:
  --min-spread N         Minimum spread in cents to act on (default: 3)
  --taker-fee N          Taker fee in cents (default: 2)
  --endgame-min N        Lower bound % for endgame strategy (default: 95)
  --endgame-max N        Upper bound % for endgame strategy (default: 99)

Risk:
  --daily-loss-cap N     Daily loss cap as % of capital (default: 5)
  --circuit-breaker N    Consecutive losses before halt (default: 5)

Execution:
  --scan-interval N      Seconds between market scans (default: 10)
  --scans N              Total number of scans to run (default: unlimited)

Logging:
  --log-level LEVEL      DEBUG | INFO | WARNING | ERROR (default: INFO)
```

`simulate.py` and `dashboard.py` share most of the same flags, plus:

```
  --tick FLOAT           Seconds between simulation ticks (default: 1.0)
  --markets N            Number of synthetic markets to generate (default: 15)
  --port N               Dashboard port (default: 5000)
```

---

## Deployment

### Raspberry Pi (Live Bot + Remote Dashboard)

The bot ran on a Raspberry Pi, with the web dashboard accessible remotely for monitoring live P&L from any device. The Pi handled both the trading process and serving the dashboard over the local network (or via a VPN/tunnel for access from outside the home).

**Setup on Raspberry Pi OS (Debian-based):**

```bash
# Install Python 3.11
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv

# Clone and install
git clone https://github.com/aphillipsmusik/polymarket-bot.git
cd polymarket-bot
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Add your private key
cp .env.example .env
nano .env  # set POLYMARKET_PRIVATE_KEY=0x...
```

**Run the bot and dashboard together:**

Run both processes so the live bot feeds P&L data while the dashboard serves it to remote browsers:

```bash
# Terminal 1 — live bot
python polymarket_bot.py --live --capital 50

# Terminal 2 — dashboard (binds to 0.0.0.0 so it's reachable on the network)
python dashboard.py --port 5000 --no-browser
```

Then open `http://<raspberry-pi-ip>:5000` from any browser on your network to monitor P&L in real time.

**Run both on boot with systemd:**

```bash
sudo nano /etc/systemd/system/polymarket-bot.service
```

```ini
[Unit]
Description=Polymarket Live Bot
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/polymarket-bot
EnvironmentFile=/home/pi/polymarket-bot/.env
ExecStart=/home/pi/polymarket-bot/venv/bin/python polymarket_bot.py \
    --live --capital 50 --log-level INFO
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=600
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo nano /etc/systemd/system/polymarket-dashboard.service
```

```ini
[Unit]
Description=Polymarket P&L Dashboard
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/polymarket-bot
ExecStart=/home/pi/polymarket-bot/venv/bin/python dashboard.py --port 5000 --no-browser
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polymarket-bot polymarket-dashboard
sudo systemctl start polymarket-bot polymarket-dashboard
```

**View logs:**

```bash
journalctl -u polymarket-bot -f
journalctl -u polymarket-dashboard -f
```

The dashboard updates in real time via Server-Sent Events — no page refresh needed. It displays equity curve, open positions, win rate, Sharpe ratio, fill rate, and a live feed of arbitrage events.

### VPS / systemd (Live Trading)

An automated setup script provisions the server, creates a virtualenv, and installs a systemd service:

```bash
sudo bash deploy/setup.sh
sudo systemctl start polymarket
journalctl -u polymarket -f
```

Recommended VPS specs:

| Tier | Cores | RAM | Storage |
|---|---|---|---|
| Lite (1–2 markets) | 4 | 8 GB | 70 GB NVMe |
| Pro (3–5 markets) | 6 | 16 GB | 100 GB NVMe |
| Ultra (multi-market) | 24 | 64 GB | 200 GB NVMe |

All tiers benefit from DDoS protection and 1 Gbps+ network for low-latency order submission.

---

## Project Structure

```
polymarket-bot/
├── polymarket_bot.py        # CLI entry point (live / dry-run)
├── simulate.py              # Offline paper-trading simulation
├── dashboard.py             # FastAPI web dashboard (SSE)
├── requirements.txt
├── .env.example
├── deploy/
│   ├── setup.sh             # VPS provisioning script
│   └── polymarket.service   # systemd unit file
└── trading/polymarket/
    ├── bot.py               # Async orchestrator
    ├── auth.py              # EIP-712 + HMAC-SHA256 authentication
    ├── config.py            # Centralised configuration dataclass
    ├── data_collector.py    # WebSocket feed + Gamma REST client
    ├── strategy_engine.py   # Four arbitrage analyzers
    ├── order_manager.py     # CLOB order placement and unwinding
    ├── risk_manager.py      # Circuit breakers and position limits
    ├── models.py            # Shared dataclasses (Market, OrderBook, …)
    ├── metrics.py           # Sharpe ratio, win rate, fill rate tracking
    └── simulation.py        # Synthetic price engine (Ornstein-Uhlenbeck)
```

---

## External APIs

| API | URL | Used for |
|---|---|---|
| Gamma REST | `https://gamma-api.polymarket.com` | Market metadata |
| CLOB REST | `https://clob.polymarket.com` | Order placement and order books |
| CLOB WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Real-time order-book updates |

---

## Performance Targets

| Metric | Target |
|---|---|
| Execution success rate | 99.2% (both FOK legs fill) |
| Response time | ~80ms |
| Fill rate per leg | 99.6% |
| Slippage | ~0.1–0.3¢ per order |
