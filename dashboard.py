"""
dashboard.py — Real-time P&L dashboard for the Polymarket paper trading simulation.

Runs the full simulation + a FastAPI web server in the same asyncio event loop.
Live updates are pushed to the browser via Server-Sent Events (SSE).

Usage
─────
    python dashboard.py                      # opens http://localhost:5000
    python dashboard.py --port 8080          # custom port
    python dashboard.py --markets 20 --tick 0.5
    python dashboard.py --scans 200
    python dashboard.py --no-browser         # don't auto-open browser

Then open: http://localhost:5000  (or the port you chose)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import webbrowser
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from trading.polymarket.config import PolymarketConfig
from trading.polymarket.models import ArbOpportunity, ArbResult
from simulate import SimulationBot

logger = logging.getLogger(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────

_subscribers: List[asyncio.Queue] = []   # one queue per open browser tab

def _snapshot_default(capital: float) -> Dict[str, Any]:
    return {
        "equity": capital, "cash": capital, "pnl": 0.0, "pnl_pct": 0.0,
        "deployed": 0.0, "trades": 0, "wins": 0, "losses": 0, "missed": 0,
        "win_rate": 0.0, "fill_rate": 100.0, "avg_response_ms": 0.0,
        "p99_response_ms": 0.0, "sharpe": 0.0,
        "total_fees": 0.0, "total_gas": 0.0, "total_slippage": 0.0,
        "open_positions": [], "closed_count": 0,
        "equity_curve": [capital], "timestamps": [_now()],
        "arb_events": [], "scan": 0, "status": "starting",
        "initial_capital": capital,
        "scan_markets": [],
    }

_state: Dict[str, Any] = {}

def _now() -> str:
    return datetime.utcnow().strftime("%H:%M:%S")

async def _broadcast(event_type: str, data: Dict) -> None:
    msg = f"data: {json.dumps({'type': event_type, **data})}\n\n"
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Polymarket Dashboard")

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/state")
async def state():
    return _state

@app.get("/events")
async def events():
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.append(q)
    # Send current state immediately on connect
    await q.put(f"data: {json.dumps({'type': 'init', **_state})}\n\n")

    async def stream() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield msg
                except asyncio.TimeoutError:
                    yield "data: {\"type\":\"ping\"}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Dashboard Bot ─────────────────────────────────────────────────────────────

class DashboardBot(SimulationBot):
    """
    SimulationBot subclass that pushes live events to the web dashboard
    after every arb execution and scan cycle.

    Pass live_data=True to use the real Polymarket API instead of synthetic data.
    """

    def __init__(self, initial_capital: float, live_data: bool = False, **kwargs) -> None:
        super().__init__(live_data=live_data, **kwargs)
        _state.update(_snapshot_default(initial_capital))

    # ── Hook: after every arb attempt ─────────────────────────────────────────

    async def _try_execute(self, opp: ArbOpportunity) -> None:
        await super()._try_execute(opp)  # includes _record_arb_success fix
        await self._push_state()

        # Build arb event entry
        snap = self.metrics.snapshot
        result = "UNKNOWN"
        profit = 0.0
        if self._arb_events:
            ev = self._arb_events[0]
            if "SUCCESS" in ev:
                result = "SUCCESS"
                profit = snap.realized_pnl
            elif "MISS" in ev:
                result = "MISS"
            elif "FAIL" in ev:
                result = "FAIL"
            elif "PARTIAL" in ev:
                result = "PARTIAL"

        event_entry = {
            "time": _now(),
            "result": result,
            "market": opp.question[:55],
            "arb_type": opp.arb_type.value.replace("_", "-"),
            "spread_pct": round(opp.gross_profit_pct * 100, 2),
            "net_profit": round(opp.net_profit_usd, 4),
        }

        # Prepend to event log (keep last 50)
        _state["arb_events"] = ([event_entry] + _state["arb_events"])[:50]
        await _broadcast("arb_event", {"event": event_entry, **self._portfolio_dict()})

    async def _on_resolution(self) -> None:
        """Push state update immediately after market resolution."""
        await self._push_state()
        await _broadcast("scan_complete", _state)

    async def _on_scan_complete(self, scan_details: list) -> None:
        """Capture per-market scan snapshot and broadcast it."""
        _state["scan_markets"] = scan_details

    # ── Hook: after every scan ─────────────────────────────────────────────────

    def _print_status(self) -> None:
        super()._print_status()
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._push_scan())
        )

    async def _push_scan(self) -> None:
        await self._push_state()
        await _broadcast("scan_complete", _state)

    # ── State builder ─────────────────────────────────────────────────────────

    async def _push_state(self) -> None:
        snap = self.metrics.snapshot
        equity = self.portfolio.equity
        initial = self.config.initial_capital
        pnl = equity - initial
        pnl_pct = pnl / initial * 100 if initial else 0

        positions = []
        for p in self.portfolio.open_positions:
            book = self.data.get_book(p.token_id)
            current = book.best_bid if book and book.best_bid else p.entry_price
            positions.append({
                "question": p.question[:48],
                "direction": p.direction,
                "size_usd": round(p.cost_basis, 2),
                "entry": round(p.entry_price, 4),
                "current": round(current, 4),
                "pnl": round(p.unrealized_pnl(current), 4),
                "arb_type": p.arb_type.value,
            })

        # Append to equity curve (cap at 500 points)
        curve = _state.get("equity_curve", [initial])
        times = _state.get("timestamps", [_now()])
        curve = (curve + [round(equity, 4)])[-500:]
        times = (times + [_now()])[-500:]

        _state.update({
            "equity": round(equity, 4),
            "cash": round(self.portfolio.cash_usdc, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "deployed": round(self.portfolio.total_cost_basis, 4),
            "trades": snap.total_trades,
            "wins": snap.successful_executions,
            "losses": snap.failed_executions,
            "missed": snap.missed_opportunities,
            "win_rate": round(snap.win_rate * 100, 1),
            "fill_rate": round(snap.execution_success_rate * 100, 1),
            "avg_response_ms": round(snap.avg_response_time_ms, 1),
            "p99_response_ms": round(snap.p99_response_time_ms, 1),
            "sharpe": round(snap.sharpe_ratio, 3),
            "total_fees": round(snap.total_fees_paid, 4),
            "total_gas": round(snap.total_gas_paid, 4),
            "total_slippage": round(snap.total_slippage, 4),
            "open_positions": positions,
            "closed_count": len(self.portfolio.closed_positions),
            "equity_curve": curve,
            "timestamps": times,
            "scan": self._scan_count,
            "status": "running",
            "scan_markets": self._last_scan_results,
        })

    def _portfolio_dict(self) -> Dict:
        return {k: _state[k] for k in
                ("equity","pnl","pnl_pct","trades","wins","losses",
                 "win_rate","fill_rate","avg_response_ms","deployed")}

    async def _run_async(self, max_scans=None) -> None:
        await super()._run_async(max_scans)
        _state["status"] = "stopped"
        await _broadcast("status", {"status": "stopped"})


# ── HTML Dashboard ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #7d8590; --green: #3fb950;
    --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
    --green-dim: #1a3a20; --red-dim: #3a1a1a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 16px; font-weight: 600; letter-spacing: 0.5px; }
  #status-badge { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; background: var(--yellow); color: #000; }
  #status-badge.running { background: var(--green); }
  #status-badge.stopped { background: var(--red); }
  .main { padding: 20px 24px; display: flex; flex-direction: column; gap: 16px; }

  /* Top metrics row */
  .metrics-row { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr 1fr; gap: 12px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 8px; }
  .card-value { font-size: 28px; font-weight: 700; line-height: 1; }
  .card-sub { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .green { color: var(--green); }
  .red { color: var(--red); }
  .muted { color: var(--muted); }

  /* Equity card */
  #equity-card { display: flex; flex-direction: column; justify-content: center; }
  #equity-val { font-size: 40px; }

  /* Chart + events row */
  .middle-row { display: grid; grid-template-columns: 1fr 380px; gap: 12px; }
  .chart-card { position: relative; height: 280px; }
  canvas { width: 100% !important; }

  /* Events feed */
  .events-card { overflow: hidden; display: flex; flex-direction: column; }
  .events-card .card-label { flex-shrink: 0; }
  #events-list { overflow-y: auto; flex: 1; max-height: 240px; }
  .event-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
  .event-row:last-child { border-bottom: none; }
  .badge { padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: 700; flex-shrink: 0; }
  .badge.SUCCESS { background: var(--green-dim); color: var(--green); }
  .badge.MISS { background: #2a2a1a; color: var(--yellow); }
  .badge.FAIL, .badge.PARTIAL { background: var(--red-dim); color: var(--red); }
  .badge.UNKNOWN { background: #1a1f2a; color: var(--blue); }
  .event-market { flex: 1; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .event-spread { color: var(--blue); flex-shrink: 0; }
  .event-profit.pos { color: var(--green); flex-shrink: 0; }
  .event-profit.neg { color: var(--red); flex-shrink: 0; }

  /* Bottom row */
  .bottom-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

  /* Section 6 metrics */
  .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .metric-item { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); }
  .metric-item:last-child { border-bottom: none; }
  .metric-name { color: var(--muted); font-size: 12px; }
  .metric-val { font-weight: 600; font-size: 13px; }
  .ok { color: var(--green); }
  .warn { color: var(--yellow); }
  .bad { color: var(--red); }

  /* Positions table */
  .positions-card { overflow: hidden; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { color: var(--muted); text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; }
  td { padding: 7px 8px; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  .empty-msg { color: var(--muted); text-align: center; padding: 20px; font-size: 12px; }

  /* Arb opportunity cards (Polymarket-style) */
  .scan-card { overflow: hidden; }
  .scan-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
  .mkt-card { background: #161b22; border: 1px solid var(--border); border-radius: 10px; padding: 14px 14px 12px; display: flex; flex-direction: column; gap: 10px; transition: border-color .15s; }
  .mkt-card:hover { border-color: rgba(63,185,80,0.5); }
  .mkt-question { font-size: 13px; font-weight: 500; line-height: 1.45; min-height: 38px; }
  .mkt-meta { display: flex; justify-content: space-between; align-items: center; }
  .mkt-spread { font-size: 11px; font-weight: 700; color: var(--green); background: var(--green-dim); padding: 2px 8px; border-radius: 4px; }
  .mkt-vol { font-size: 10px; color: var(--muted); }
  .mkt-btns { display: flex; gap: 8px; }
  .btn-yes { flex: 1; background: rgba(63,185,80,0.12); border: 1px solid rgba(63,185,80,0.3); color: var(--green); border-radius: 7px; padding: 7px 4px 5px; text-align: center; }
  .btn-no  { flex: 1; background: rgba(248,81,73,0.10); border: 1px solid rgba(248,81,73,0.25); color: var(--red);   border-radius: 7px; padding: 7px 4px 5px; text-align: center; }
  .btn-label { font-size: 9px; font-weight: 400; opacity: .65; text-transform: uppercase; letter-spacing: .5px; display: block; margin-bottom: 2px; }
  .btn-pct { font-size: 14px; font-weight: 700; }

  /* Pulse animation for live indicator */
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  #live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); animation: pulse 2s infinite; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-track { background: transparent; } ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
</style>
</head>
<body>
<header>
  <div id="live-dot"></div>
  <h1>POLYMARKET PAPER TRADING</h1>
  <span id="status-badge">CONNECTING</span>
  <span style="margin-left:auto;color:var(--muted);font-size:12px">Last update: <span id="last-update">—</span></span>
</header>

<div class="main">
  <!-- Top row: equity + 4 stat cards -->
  <div class="metrics-row">
    <div class="card" id="equity-card">
      <div class="card-label">Portfolio Equity</div>
      <div class="card-value" id="equity-val">$—</div>
      <div class="card-sub">
        <span id="pnl-val">—</span> &nbsp;|&nbsp; <span id="pnl-pct">—</span>
        &nbsp;|&nbsp; Cash: <span id="cash-val">—</span>
      </div>
    </div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value" id="win-rate">—</div>
      <div class="card-sub"><span id="trades-count">0</span> trades · <span id="wins-count">0</span>W / <span id="losses-count">0</span>L</div>
    </div>
    <div class="card">
      <div class="card-label">Fill Rate</div>
      <div class="card-value" id="fill-rate">—</div>
      <div class="card-sub">Target: 99.2%</div>
    </div>
    <div class="card">
      <div class="card-label">Avg Response</div>
      <div class="card-value" id="avg-response">—</div>
      <div class="card-sub">Target: ~80ms</div>
    </div>
    <div class="card">
      <div class="card-label">Scan #</div>
      <div class="card-value muted" id="scan-num">—</div>
      <div class="card-sub"><span id="open-count">0</span> open · <span id="closed-count">0</span> closed</div>
    </div>
  </div>

  <!-- Chart + event feed -->
  <div class="middle-row">
    <div class="card chart-card">
      <div class="card-label" style="margin-bottom:12px">Equity Curve</div>
      <canvas id="equity-chart" height="220"></canvas>
    </div>
    <div class="card events-card">
      <div class="card-label">Arb Events</div>
      <div id="events-list">
        <div class="empty-msg">Waiting for arb opportunities…</div>
      </div>
    </div>
  </div>

  <!-- Section 6 metrics + positions -->
  <div class="bottom-row">
    <div class="card">
      <div class="card-label" style="margin-bottom:12px">Section 6 Metrics</div>
      <div class="metrics-grid">
        <div>
          <div class="metric-item"><span class="metric-name">Realized P&L</span><span class="metric-val" id="m-pnl">—</span></div>
          <div class="metric-item"><span class="metric-name">Total Fees</span><span class="metric-val bad" id="m-fees">—</span></div>
          <div class="metric-item"><span class="metric-name">Gas Paid</span><span class="metric-val bad" id="m-gas">—</span></div>
          <div class="metric-item"><span class="metric-name">Slippage</span><span class="metric-val warn" id="m-slip">—</span></div>
        </div>
        <div>
          <div class="metric-item"><span class="metric-name">Sharpe Ratio</span><span class="metric-val" id="m-sharpe">—</span></div>
          <div class="metric-item"><span class="metric-name">P99 Response</span><span class="metric-val" id="m-p99">—</span></div>
          <div class="metric-item"><span class="metric-name">Missed Opps</span><span class="metric-val warn" id="m-missed">—</span></div>
          <div class="metric-item"><span class="metric-name">Deployed</span><span class="metric-val" id="m-deployed">—</span></div>
        </div>
      </div>
    </div>
    <div class="card positions-card">
      <div class="card-label" style="margin-bottom:10px">Open Positions</div>
      <div id="positions-container">
        <div class="empty-msg">No open positions</div>
      </div>
    </div>
  </div>

  <!-- Arb opportunities — full width, Polymarket-style cards -->
  <div class="card scan-card">
    <div class="card-label" style="margin-bottom:12px">
      Arb Opportunities
      <span style="margin-left:8px;color:var(--muted);font-size:10px;font-weight:400">viable markets from last scan · spread = 1 − YES ask − NO ask</span>
    </div>
    <div id="scan-container">
      <div class="empty-msg">Waiting for first scan…</div>
    </div>
  </div>
</div>

<script>
// ── Chart setup ───────────────────────────────────────────────────────────────
const ctx = document.getElementById('equity-chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      data: [],
      borderColor: '#58a6ff',
      backgroundColor: 'rgba(88,166,255,0.08)',
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
      fill: true,
    }]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    animation: { duration: 300 },
    plugins: { legend: { display: false }, tooltip: {
      callbacks: { label: ctx => '$' + ctx.parsed.y.toFixed(4) }
    }},
    scales: {
      x: { display: false },
      y: {
        grid: { color: '#21262d' },
        ticks: { color: '#7d8590', callback: v => '$' + v.toFixed(2) }
      }
    }
  }
});

// ── State helpers ─────────────────────────────────────────────────────────────
function fmt$(v)  { return '$' + Math.abs(v).toFixed(4); }
function fmtPct(v){ return (v >= 0 ? '+' : '') + v.toFixed(3) + '%'; }
function cls(v)   { return v > 0 ? 'green' : v < 0 ? 'red' : 'muted'; }

function applyState(s) {
  if (!s || !s.equity) return;

  // Equity
  const pnl = s.pnl || 0;
  document.getElementById('equity-val').textContent = '$' + s.equity.toFixed(4);
  document.getElementById('equity-val').className = 'card-value ' + cls(pnl);
  document.getElementById('pnl-val').textContent = (pnl >= 0 ? '+' : '') + fmt$(pnl);
  document.getElementById('pnl-val').className = cls(pnl);
  document.getElementById('pnl-pct').textContent = fmtPct(s.pnl_pct || 0);
  document.getElementById('pnl-pct').className = cls(pnl);
  document.getElementById('cash-val').textContent = '$' + (s.cash || 0).toFixed(2);

  // Stats
  const wr = s.win_rate || 0;
  document.getElementById('win-rate').textContent = wr.toFixed(1) + '%';
  document.getElementById('win-rate').className = 'card-value ' + (wr >= 60 ? 'green' : wr >= 40 ? 'warn' : 'red');
  document.getElementById('trades-count').textContent = s.trades || 0;
  document.getElementById('wins-count').textContent = s.wins || 0;
  document.getElementById('losses-count').textContent = s.losses || 0;

  const fr = s.fill_rate || 0;
  document.getElementById('fill-rate').textContent = fr.toFixed(1) + '%';
  document.getElementById('fill-rate').className = 'card-value ' + (fr >= 99.2 ? 'ok' : fr >= 95 ? 'warn' : 'bad');

  const rt = s.avg_response_ms || 0;
  document.getElementById('avg-response').textContent = rt.toFixed(0) + 'ms';
  document.getElementById('avg-response').className = 'card-value ' + (rt <= 80 ? 'ok' : rt <= 200 ? 'warn' : 'bad');

  document.getElementById('scan-num').textContent = s.scan || 0;
  document.getElementById('open-count').textContent = (s.open_positions || []).length;
  document.getElementById('closed-count').textContent = s.closed_count || 0;

  // Section 6 metrics
  document.getElementById('m-pnl').textContent = (pnl >= 0 ? '+' : '') + fmt$(pnl);
  document.getElementById('m-pnl').className = 'metric-val ' + cls(pnl);
  document.getElementById('m-fees').textContent = fmt$(s.total_fees || 0);
  document.getElementById('m-gas').textContent = fmt$(s.total_gas || 0);
  document.getElementById('m-slip').textContent = fmt$(s.total_slippage || 0);
  document.getElementById('m-sharpe').textContent = (s.sharpe || 0).toFixed(3);
  document.getElementById('m-p99').textContent = (s.p99_response_ms || 0).toFixed(0) + 'ms';
  document.getElementById('m-missed').textContent = s.missed || 0;
  document.getElementById('m-deployed').textContent = '$' + (s.deployed || 0).toFixed(2);

  // Equity chart
  if (s.equity_curve && s.equity_curve.length > 0) {
    chart.data.labels = s.timestamps || s.equity_curve.map((_, i) => i);
    chart.data.datasets[0].data = s.equity_curve;
    // Color line based on overall P&L
    chart.data.datasets[0].borderColor = pnl >= 0 ? '#3fb950' : '#f85149';
    chart.data.datasets[0].backgroundColor = pnl >= 0 ? 'rgba(63,185,80,0.08)' : 'rgba(248,81,73,0.08)';
    chart.update('none');
  }

  // Positions table
  const posEl = document.getElementById('positions-container');
  const positions = s.open_positions || [];
  if (positions.length === 0) {
    posEl.innerHTML = '<div class="empty-msg">No open positions</div>';
  } else {
    posEl.innerHTML = '<table><thead><tr>' +
      '<th>Market</th><th>Side</th><th>Cost</th><th>Entry</th><th>Mark</th><th>P&amp;L</th><th>%</th>' +
      '</tr></thead><tbody>' +
      positions.map(p => {
        const pc = cls(p.pnl);
        const pnlPct = p.size_usd > 0 ? (p.pnl / p.size_usd * 100) : 0;
        return `<tr>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.question}</td>
          <td style="color:${p.direction==='YES'?'var(--green)':'var(--red)'}">${p.direction}</td>
          <td>$${p.size_usd}</td>
          <td style="color:var(--muted)">${p.entry}</td>
          <td>${p.current}</td>
          <td class="${pc}">${p.pnl >= 0 ? '+' : ''}$${Math.abs(p.pnl).toFixed(4)}</td>
          <td class="${pc}" style="font-size:11px">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%</td>
        </tr>`;
      }).join('') + '</tbody></table>';
  }

  // Scan markets table
  renderScanMarkets(s.scan_markets || []);

  // Events feed
  if (s.arb_events && s.arb_events.length > 0) {
    renderEvents(s.arb_events);
  }

  document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
}

function renderScanMarkets(markets) {
  const el = document.getElementById('scan-container');
  const arb = (markets || []).filter(m => m.arb);
  if (arb.length === 0) {
    el.innerHTML = '<div class="empty-msg">No arb opportunities in last scan — watching…</div>';
    return;
  }
  el.innerHTML = '<div class="scan-grid">' +
    arb.map(m => {
      const yesPct = (m.yes * 100).toFixed(1);
      const noPct  = (m.no  * 100).toFixed(1);
      return `<div class="mkt-card">
        <div class="mkt-question">${m.q}</div>
        <div class="mkt-meta">
          <span class="mkt-vol">Sum: ${m.sum.toFixed(4)}</span>
          <span class="mkt-spread">+${m.spread_pct.toFixed(2)}% spread</span>
        </div>
        <div class="mkt-btns">
          <div class="btn-yes"><span class="btn-label">Yes</span><span class="btn-pct">${yesPct}%</span></div>
          <div class="btn-no"><span class="btn-label">No</span><span class="btn-pct">${noPct}%</span></div>
        </div>
      </div>`;
    }).join('') + '</div>';
}

function renderEvents(events) {
  const el = document.getElementById('events-list');
  el.innerHTML = events.map(e => {
    const profitHtml = e.result === 'SUCCESS'
      ? `<span class="event-profit pos">+$${e.net_profit.toFixed(4)}</span>`
      : `<span class="event-profit neg">$${e.net_profit.toFixed(4)}</span>`;
    return `<div class="event-row">
      <span class="muted" style="font-size:10px;flex-shrink:0">${e.time}</span>
      <span class="badge ${e.result}">${e.result}</span>
      <span class="event-market">${e.market}</span>
      <span class="event-spread">${e.spread_pct}%</span>
      ${profitHtml}
    </div>`;
  }).join('');
}

// ── SSE connection ────────────────────────────────────────────────────────────
function connect() {
  const es = new EventSource('/events');

  es.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'ping') return;

    // Update status badge
    const badge = document.getElementById('status-badge');
    if (data.status === 'running' || data.type === 'arb_event' || data.type === 'scan_complete' || data.type === 'init') {
      badge.textContent = 'LIVE';
      badge.className = 'running';
    } else if (data.status === 'stopped') {
      badge.textContent = 'STOPPED';
      badge.className = 'stopped';
      document.getElementById('live-dot').style.animationPlayState = 'paused';
    }

    // For arb events: only update the event feed + top stats, not positions/cash
    if (data.type === 'arb_event' && data.event) {
      const el = document.getElementById('events-list');
      const ev = data.event;
      const profitHtml = ev.result === 'SUCCESS'
        ? `<span class="event-profit pos">+$${ev.net_profit.toFixed(4)}</span>`
        : `<span class="event-profit neg">$${ev.net_profit.toFixed(4)}</span>`;
      const row = `<div class="event-row">
        <span class="muted" style="font-size:10px;flex-shrink:0">${ev.time}</span>
        <span class="badge ${ev.result}">${ev.result}</span>
        <span class="event-market">${ev.market}</span>
        <span class="event-spread">${ev.spread_pct}%</span>
        ${profitHtml}
      </div>`;
      if (el.querySelector('.empty-msg')) el.innerHTML = '';
      el.insertAdjacentHTML('afterbegin', row);
      while (el.children.length > 50) el.removeChild(el.lastChild);
      // Don't call applyState for arb_event — positions/cash update on scan_complete
      return;
    }

    applyState(data);
  };

  es.onerror = () => {
    document.getElementById('status-badge').textContent = 'RECONNECTING';
    document.getElementById('status-badge').className = '';
    setTimeout(connect, 3000);
    es.close();
  };
}

connect();
</script>
</body>
</html>"""


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dashboard",
        description="Polymarket paper trading dashboard (real-time P&L)",
    )
    p.add_argument("--port",      type=int,   default=5000,  help="Web server port (default: 5000)")
    p.add_argument("--no-browser", action="store_true",    help="Don't auto-open browser")
    p.add_argument("--simulate", action="store_true",
                   help="Use synthetic offline data instead of live Polymarket markets")
    p.add_argument("--tick",    type=float, default=1.0,   help="WS tick interval seconds (default: 1.0)")
    p.add_argument("--markets", type=int,   default=15,    help="Synthetic markets 1-20 (default: 15)")
    p.add_argument("--seed",    type=int,   default=42,    help="RNG seed (default: 42)")
    p.add_argument("--capital", type=float, default=50.0,  help="Starting USDC (default: 50)")
    p.add_argument("--max-bet", type=float, default=5.0,   help="Max bet USDC (default: 5)")
    p.add_argument("--max-position-pct", type=float, default=5.0)
    p.add_argument("--max-exposure-pct", type=float, default=20.0)
    p.add_argument("--min-spread",  type=float, default=3.0)
    p.add_argument("--taker-fee",   type=float, default=2.0)
    p.add_argument("--scan-interval", type=int, default=10)
    p.add_argument("--scans",   type=int,   default=None,  help="Stop after N scans")
    p.add_argument("--log-level", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout,
    )


async def _main_async(args: argparse.Namespace) -> None:
    config = PolymarketConfig(
        initial_capital=args.capital,
        max_bet_usd=args.max_bet,
        max_position_pct=args.max_position_pct / 100.0,
        max_portfolio_exposure_pct=args.max_exposure_pct / 100.0,
        s2o_min_spread_pct=args.min_spread / 100.0,
        taker_fee_rate=args.taker_fee / 100.0,
        scan_interval_s=args.scan_interval,
        dry_run=True,
        log_level=args.log_level,
    )

    bot = DashboardBot(
        initial_capital=args.capital,
        live_data=not args.simulate,    # live data by default; --simulate for offline
        config=config,
        tick_interval_s=args.tick,
        num_markets=min(args.markets, 20),
        seed=args.seed,
    )

    # Run uvicorn in background task (same event loop)
    uv_config = uvicorn.Config(
        app, host="0.0.0.0", port=args.port,
        log_level="warning", loop="none",
    )
    server = uvicorn.Server(uv_config)
    api_task = asyncio.create_task(server.serve(), name="dashboard_api")

    # Give the server a moment to start then open browser
    await asyncio.sleep(1.0)
    url = f"http://localhost:{args.port}"
    if not args.no_browser:
        webbrowser.open(url)
    print(f"\n  Dashboard running at {url}\n  Press Ctrl+C to stop\n")

    try:
        await bot._run_async(max_scans=args.scans)
    finally:
        server.should_exit = True
        api_task.cancel()
        try:
            await api_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
