"""
data_collector.py — Section 3, Component 1: Data Collector (WebSocket/REST).

Responsibilities
----------------
1. WebSocket feed   — subscribes to CLOB market channel for real-time
                      order-book snapshots and price-change deltas.
                      Ping: 10s (CLOB), auto-reconnects on drop.
2. REST market scan — polls Gamma API for active markets (with filters).
3. Rate limiter     — enforces Section 1 limits:
                        100 req/min (public REST)
                        60  orders/min (handled by OrderManager)
4. In-memory cache  — keeps latest OrderBookSnapshot per token_id so
                      the Strategy Engine can query books without I/O.

Section 3 APIs used:
  Gamma API   → market metadata (question, end date, liquidity, volume)
  CLOB API    → order book data (real-time via WebSocket + REST fallback)
  Data API    → positions and trade history (REST)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Callable, Coroutine, Dict, List, Optional, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import PolymarketConfig
from .models import Market, OrderBookSnapshot, OrderLevel, Token

logger = logging.getLogger(__name__)

BookUpdateCallback = Callable[[str, OrderBookSnapshot], Optional[Coroutine]]


# ── Rate limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Token-bucket rate limiter.
    Enforces Section 1: 100 req/min for public endpoints.
    """

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._window = deque()           # timestamps of recent calls

    def acquire(self) -> None:
        """Block (sync) until a token is available."""
        now = time.monotonic()
        # Drop entries older than 60 s
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()

        if len(self._window) >= self._max:
            sleep_for = 60.0 - (now - self._window[0]) + 0.01
            logger.debug(f"Rate limit: sleeping {sleep_for:.2f}s")
            time.sleep(sleep_for)
            # Re-clean after sleep
            now = time.monotonic()
            while self._window and now - self._window[0] > 60.0:
                self._window.popleft()

        self._window.append(time.monotonic())

    async def acquire_async(self) -> None:
        """Async-compatible rate-limit acquire (yields to event loop while waiting)."""
        now = time.monotonic()
        while self._window and now - self._window[0] > 60.0:
            self._window.popleft()

        if len(self._window) >= self._max:
            sleep_for = 60.0 - (now - self._window[0]) + 0.01
            logger.debug(f"Rate limit (async): sleeping {sleep_for:.2f}s")
            await asyncio.sleep(sleep_for)
            now = time.monotonic()
            while self._window and now - self._window[0] > 60.0:
                self._window.popleft()

        self._window.append(time.monotonic())


# ── HTTP session factory ──────────────────────────────────────────────────────

def _make_session(retries: int = 3) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "POST"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"Accept": "application/json", "User-Agent": "polymarket-bot/2.0"})
    return session


# ── WebSocket client ──────────────────────────────────────────────────────────

class ClobWebSocket:
    """
    Real-time order-book feed via Polymarket CLOB WebSocket.

    Section 3 note: ping interval 10s (CLOB), 5s (Real-Time Stream).
    Maintains full in-memory order book per token via snapshot + delta updates.
    """

    def __init__(
        self,
        config: PolymarketConfig,
        on_book_update: Optional[BookUpdateCallback] = None,
    ) -> None:
        self.config = config
        self.on_book_update = on_book_update
        self._books: Dict[str, OrderBookSnapshot] = {}
        self._subscribed: Set[str] = set()
        self._ws = None
        self._connected = False
        self._stop = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        return self._books.get(token_id)

    def stop(self) -> None:
        self._stop = True

    async def connect(self) -> None:
        """Run WebSocket connection forever with auto-reconnect."""
        try:
            import websockets
        except ImportError:
            raise ImportError("pip install websockets")

        while not self._stop:
            try:
                logger.info(f"WS connecting → {self.config.clob_ws_url}")
                async with websockets.connect(
                    self.config.clob_ws_url,
                    ping_interval=self.config.clob_ws_ping_s,
                    ping_timeout=self.config.clob_ws_ping_s * 2,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("WS connected")
                    if self._subscribed:
                        await self._send_sub(ws, list(self._subscribed))
                    async for msg in ws:
                        if self._stop:
                            break
                        await self._dispatch(msg)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(f"WS error: {exc!r} — reconnecting in {self.config.ws_reconnect_delay_s}s")
                self._connected = False
                self._ws = None
                await asyncio.sleep(self.config.ws_reconnect_delay_s)

    async def add_subscriptions(self, token_ids: List[str]) -> None:
        new = set(token_ids) - self._subscribed
        if not new:
            return
        self._subscribed.update(new)
        if self._ws and self._connected:
            await self._send_sub(self._ws, list(new))

    @staticmethod
    async def _send_sub(ws, token_ids: List[str]) -> None:
        msg = json.dumps({"type": "subscribe", "channel": "market", "markets": token_ids})
        await ws.send(msg)
        logger.debug(f"WS subscribed: {len(token_ids)} tokens")

    async def _dispatch(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        events = payload if isinstance(payload, list) else [payload]
        for ev in events:
            etype = ev.get("event_type", "")
            if etype == "book":
                await self._on_snapshot(ev)
            elif etype == "price_change":
                await self._on_delta(ev)

    async def _on_snapshot(self, ev: dict) -> None:
        tid = ev.get("asset_id", "")
        if not tid:
            return
        bids = sorted(
            [OrderLevel(float(b["price"]), float(b["size"])) for b in ev.get("bids", []) if float(b.get("size",0))>0],
            key=lambda x: -x.price,
        )
        asks = sorted(
            [OrderLevel(float(a["price"]), float(a["size"])) for a in ev.get("asks", []) if float(a.get("size",0))>0],
            key=lambda x: x.price,
        )
        self._books[tid] = OrderBookSnapshot(token_id=tid, bids=bids, asks=asks)
        await _fire(self.on_book_update, tid, self._books[tid])

    async def _on_delta(self, ev: dict) -> None:
        tid = ev.get("asset_id", "")
        if not tid or tid not in self._books:
            return
        book = self._books[tid]
        for ch in ev.get("changes", []):
            try:
                p, s, side = float(ch["price"]), float(ch["size"]), ch.get("side","").upper()
            except (KeyError, ValueError):
                continue
            if side in ("BUY", "BID"):
                _apply_level(book.bids, p, s, desc=True)
            elif side in ("SELL", "ASK"):
                _apply_level(book.asks, p, s, desc=False)
        await _fire(self.on_book_update, tid, book)


# ── REST market data collector ────────────────────────────────────────────────

class GammaRestClient:
    """
    Polls Gamma API for market metadata.
    Applies Section 3 rate limit: 100 req/min.
    """

    def __init__(self, config: PolymarketConfig) -> None:
        self.config = config
        self._session = _make_session(retries=config.max_api_retries)
        self._limiter = RateLimiter(config.public_rate_limit_per_min)

    def get_active_markets(self, limit: int = 200) -> List[Market]:
        markets: List[Market] = []
        cursor = ""
        for _ in range(10):
            self._limiter.acquire()
            params = {
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": str(min(limit - len(markets), 100)),
            }
            if cursor:
                params["next_cursor"] = cursor
            try:
                r = self._session.get(
                    f"{self.config.gamma_host}/markets", params=params, timeout=15
                )
                r.raise_for_status()
                data = r.json()
            except requests.RequestException as exc:
                logger.warning(f"Gamma API: {exc}")
                break
            raw_list = data if isinstance(data, list) else data.get("data", [])
            cursor = data.get("next_cursor", "") if isinstance(data, dict) else ""
            for raw in raw_list:
                m = _parse_market(raw)
                if m:
                    markets.append(m)
            if len(markets) >= limit or not cursor:
                break
        logger.info(f"Gamma API: {len(markets)} markets fetched")
        return markets[:limit]

    def get_order_book_rest(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """REST fallback for when WS hasn't received a snapshot yet."""
        self._limiter.acquire()
        try:
            r = self._session.get(
                f"{self.config.clob_host}/book",
                params={"token_id": token_id},
                timeout=8,
            )
            r.raise_for_status()
            data = r.json()
            bids = sorted(
                [OrderLevel(float(b["price"]), float(b["size"])) for b in data.get("bids",[]) if float(b.get("size",0))>0],
                key=lambda x: -x.price,
            )
            asks = sorted(
                [OrderLevel(float(a["price"]), float(a["size"])) for a in data.get("asks",[]) if float(a.get("size",0))>0],
                key=lambda x: x.price,
            )
            return OrderBookSnapshot(token_id=token_id, bids=bids, asks=asks)
        except Exception as exc:
            logger.debug(f"REST book fallback failed ({token_id[:12]}): {exc}")
            return None

    def get_positions(self, wallet: str) -> List[dict]:
        """Section 3: Data API — positions/history."""
        self._limiter.acquire()
        try:
            r = self._session.get(
                f"{self.config.clob_host}/data/positions",
                params={"user": wallet},
                timeout=10,
            )
            r.raise_for_status()
            return r.json() or []
        except Exception:
            return []


# ── Unified DataCollector ─────────────────────────────────────────────────────

class DataCollector:
    """
    Section 3, Component 1: Data Collector.

    Combines WebSocket real-time feed + Gamma REST market scan into a single
    interface for the Strategy Engine.

    Usage (inside asyncio event loop):
        dc = DataCollector(config, on_book_update=handler)
        asyncio.create_task(dc.ws.connect())
        markets = dc.get_active_markets()
        await dc.ws.add_subscriptions(token_ids)
        book = dc.get_book(token_id)   # always latest
    """

    def __init__(
        self,
        config: PolymarketConfig,
        on_book_update: Optional[BookUpdateCallback] = None,
    ) -> None:
        self.config = config
        self.ws  = ClobWebSocket(config=config, on_book_update=on_book_update)
        self.rest = GammaRestClient(config=config)

    def get_active_markets(self, limit: int = 200) -> List[Market]:
        return self.rest.get_active_markets(limit=limit)

    def get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """
        Return the freshest available order-book snapshot for a token.

        Priority:
          1. WS cache — if present and younger than book_max_age_s, use it.
          2. REST fallback — if WS cache is absent or stale, fetch a live snapshot.
        """
        book = self.ws.get_book(token_id)
        if book is not None:
            age_s = (datetime.utcnow() - book.timestamp).total_seconds()
            if age_s <= self.config.book_max_age_s:
                return book
            logger.debug(
                f"Book stale ({age_s:.1f}s > {self.config.book_max_age_s}s) "
                f"for {token_id[:12]} — fetching REST fallback"
            )
        return self.rest.get_order_book_rest(token_id)

    async def subscribe_markets(self, markets: List[Market]) -> int:
        """Subscribe all token IDs for a list of markets. Returns count of new tokens."""
        tids = []
        for m in markets:
            tids.append(m.yes_token.token_id)
            tids.append(m.no_token.token_id)
        before = len(self.ws._subscribed)
        await self.ws.add_subscriptions(tids)
        return len(self.ws._subscribed) - before


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_level(levels: List[OrderLevel], price: float, size: float, desc: bool) -> None:
    for i, l in enumerate(levels):
        if abs(l.price - price) < 1e-8:
            if size <= 0:
                levels.pop(i)
            else:
                levels[i] = OrderLevel(price, size)
            return
    if size > 0:
        levels.append(OrderLevel(price, size))
        levels.sort(key=lambda x: (-x.price if desc else x.price))


async def _fire(cb, *args) -> None:
    if cb is None:
        return
    try:
        res = cb(*args)
        if asyncio.iscoroutine(res):
            await res
    except Exception as exc:
        logger.error(f"DataCollector callback error: {exc}", exc_info=True)


def _parse_market(raw: dict) -> Optional[Market]:
    try:
        toks = raw.get("tokens", [])
        if len(toks) < 2:
            return None
        yes = next((t for t in toks if t.get("outcome","").lower() in ("yes","true")), toks[0])
        no  = next((t for t in toks if t.get("outcome","").lower() in ("no","false")), toks[-1])
        end = None
        end_raw = raw.get("end_date_iso") or raw.get("endDateIso")
        if end_raw:
            try:
                end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        return Market(
            condition_id=raw.get("condition_id") or raw.get("conditionId",""),
            question=raw.get("question",""),
            description=raw.get("description",""),
            end_date=end,
            yes_token=Token(yes.get("token_id",""), yes.get("outcome","Yes"), float(yes.get("price",0.5))),
            no_token=Token(no.get("token_id",""),  no.get("outcome","No"),  float(no.get("price",0.5))),
            liquidity=float(raw.get("liquidity",0)),
            volume_24h=float(raw.get("volume24hr") or raw.get("volume_24hr",0)),
            active=bool(raw.get("active",True)),
            closed=bool(raw.get("closed",False)),
            accepting_orders=bool(raw.get("accepting_orders") or raw.get("acceptingOrders",True)),
        )
    except Exception as exc:
        logger.debug(f"Market parse: {exc}")
        return None
