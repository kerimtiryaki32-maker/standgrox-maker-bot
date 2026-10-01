"""StandX single-market maker bot with a local desktop dashboard.

Requires the existing standx_client.py and its .env credentials.
Run with: py main.py
"""

import time
import json
import uuid
import os
import sys
import queue
import threading
from collections import deque
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
import webbrowser

try:
    import websocket
except ImportError:
    websocket = None

# The external .env belongs beside the script or the built Windows executable.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
os.chdir(APP_DIR)

def bundled_asset(name):
    return Path(getattr(sys, "_MEIPASS", APP_DIR)) / name

from standx_client import StandXClient


SYMBOL = "BTC-USD"
TARGET_BPS = Decimal("5.5")
MIN_BPS = Decimal("5")
MAX_BPS = Decimal("6")
LOOP_SECONDS = 3
ONE_SIDED_RESET_SECONDS = 15
MARKET_DATA_MAX_AGE = 3.0             # local receipt age; stale feeds pull entries
MARKET_BACKUP_SECONDS = 1.0          # refresh missing/delayed public feeds over HTTP
GUARD_CHECK_SECONDS = 0.1
MAX_MARGIN_FRACTION = Decimal("0.10")  # at most 10% of free cross balance
MAX_LEVERAGE = 10                       # refuse to run if account is above this
EARLY_PULL_BPS = Decimal("0.25")        # pull at MIN_BPS + this buffer
BOOK_GUARD_BPS = Decimal("1")          # cancel when executable opposite quote approaches
MAX_LOSS_FRACTION = Decimal("0.02")    # account-equity emergency exit threshold
EXIT_MAKER_WAIT_SECONDS = 5             # try maker exit, then cancel and exit remainder
DRY_RUN = False                         # live trading; orders may execute
PREFIX = "SM55-"


def D(value):
    return Decimal(str(value))


def quantize_step(value, decimals, rounding=ROUND_DOWN):
    step = Decimal(1).scaleb(-decimals)
    return D(value).quantize(step, rounding=rounding)


def unwrap(data):
    if isinstance(data, dict) and data.get("code", 0) != 0:
        raise RuntimeError(f"StandX API error: {data.get('message', data.get('code'))}")
    return data.get("result", data) if isinstance(data, dict) else data


class QuoteUnavailable(RuntimeError):
    """Market snapshots temporarily disagree; wait for a fresh quote."""


class UptimeMeter:
    """Conservative local estimate; official StandX scoring is tick-level."""

    def __init__(self):
        self.intervals = deque()
        self.last_at = None
        self.last_qualified = False

    def observe(self, mark, position_qty, orders, now=None):
        now = time.monotonic() if now is None else now
        if self.last_at is not None and self.last_qualified:
            # Do not count long API gaps as qualifying time without evidence.
            self.intervals.append((self.last_at, min(now, self.last_at + 10)))
        cutoff = now - 3600
        while self.intervals and self.intervals[0][1] < cutoff:
            self.intervals.popleft()
        qualified_sides = set()
        if D(position_qty) == 0 and D(mark) > 0:
            for order in orders:
                if (order.get("symbol") != SYMBOL or order.get("reduce_only")
                        or str(order.get("status", "")).lower() != "open"):
                    continue
                side = order.get("side")
                price = D(order["price"])
                distance = ((D(mark) - price) if side == "buy" else
                            (price - D(mark))) / D(mark) * 10000
                if side in ("buy", "sell") and 0 <= distance < 10 and D(order.get("qty", 0)) > 0:
                    qualified_sides.add(side)
        self.last_at = now
        self.last_qualified = qualified_sides == {"buy", "sell"}
        return sum(max(0, end - max(start, cutoff)) for start, end in self.intervals) / 60


class MakerBot:
    def __init__(self, log=print, status=None, stop_event=None):
        self.log = log
        self.status = status or (lambda data: None)
        self.stop_event = stop_event or threading.Event()
        self.wake_event = threading.Event()
        self.stream = None
        self.quote_snapshot = {}
        self.market_lock = threading.RLock()
        self.entry_io_lock = threading.RLock()
        self.guard_wake = threading.Event()
        self.guard_stop = threading.Event()
        self.guard_thread = None
        self.market_backup_thread = None
        self.live_mark = None
        self.live_book = None
        self.mark_received_at = 0.0
        self.book_received_at = 0.0
        self.guard_entries = {}
        self.cancel_requests = set()
        self.guard_position = Decimal(0)
        self.client = StandXClient()
        self.info = self.get_info()
        self.price_decimals = int(self.info["price_tick_decimals"])
        self.qty_decimals = int(self.info["qty_tick_decimals"])
        self.min_qty = D(self.info["min_order_qty"])
        self.entry_submitted = False
        self.pair_quote_qty = None
        self.exit_submitted = False
        self.active_entry_ids = set()
        self.pending_entry_cancels = {}
        self.one_sided_since = None
        self.one_sided_side = None
        self.filled_entry_ids = set()
        self.entry_fill_count = 0
        self.uptime_meter = UptimeMeter()
        self.start_equity = D(self.get_balance()["equity"])
        if self.start_equity <= 0:
            raise RuntimeError("Account equity must be positive")
        position_config = unwrap(self.client._get(
            "/api/query_position_config", {"symbol": SYMBOL}, auth=True
        ))
        if isinstance(position_config, list):
            position_config = position_config[0]
        self.leverage = int(position_config["leverage"])
        if self.leverage > MAX_LEVERAGE:
            raise RuntimeError(
                f"Account leverage is {self.leverage}x. Set it to {MAX_LEVERAGE}x "
                "or less in StandX before using this bot."
            )
        if position_config.get("margin_mode") != "cross":
            raise RuntimeError("This version requires cross margin mode")

    def get_info(self):
        data = unwrap(self.client._get(
            "/api/query_symbol_info", {"symbol": SYMBOL}, auth=False
        ))
        if isinstance(data, list):
            data = next(x for x in data if x["symbol"] == SYMBOL)
        if data.get("enabled") is False or data.get("status") not in (None, "trading"):
            raise RuntimeError(f"Market is not trading: {data.get('status')}")
        return data

    @staticmethod
    def book_top(data):
        bids = [D(level[0]) for level in data["bids"] if D(level[1]) > 0]
        asks = [D(level[0]) for level in data["asks"] if D(level[1]) > 0]
        if not bids or not asks:
            raise QuoteUnavailable("Empty order book")
        best_bid, best_ask = max(bids), min(asks)
        if not (best_bid.is_finite() and best_ask.is_finite()
                and Decimal(0) < best_bid < best_ask):
            raise QuoteUnavailable("Crossed or invalid order book")
        return best_bid, best_ask

    def live_snapshot(self):
        now = time.monotonic()
        with self.market_lock:
            if (self.live_mark is None or self.live_book is None
                    or now - self.mark_received_at > MARKET_DATA_MAX_AGE
                    or now - self.book_received_at > MARKET_DATA_MAX_AGE):
                raise QuoteUnavailable("Waiting for fresh live mark and order book")
            return self.live_mark, *self.live_book

    def invalidate_market(self):
        with self.market_lock:
            self.live_mark = None
            self.live_book = None
        self.guard_wake.set()
        self.wake_event.set()

    def refresh_public_snapshot(self):
        """Refresh delayed feeds without treating old HTTP responses as fresh."""
        started = time.monotonic()
        price = unwrap(self.client._get(
            "/api/query_symbol_price", {"symbol": SYMBOL}, auth=False))
        mark = D(price["mark_price"])
        book = self.book_top(unwrap(self.client._get(
            "/api/query_depth_book", {"symbol": SYMBOL}, auth=False)))
        if (not mark.is_finite() or mark <= 0
                or time.monotonic() - started >= MARKET_DATA_MAX_AGE):
            raise QuoteUnavailable("HTTP market snapshot invalid or too slow")
        with self.market_lock:
            # Never replace a newer WebSocket update with an earlier HTTP read.
            if self.mark_received_at <= started:
                self.live_mark = mark
                self.mark_received_at = started
            if self.book_received_at <= started:
                self.live_book = book
                self.book_received_at = started
        self.guard_wake.set()
        self.wake_event.set()

    def on_stream_message(self, raw):
        try:
            event = json.loads(raw)
            channel, data = event.get("channel"), event.get("data")
            if not isinstance(data, dict):
                return
            if channel == "auth":
                if data.get("code") not in (0, 200, "0", "200"):
                    self.log(f"WebSocket authentication failed (code {data.get('code')}); reconnecting")
                    self.invalidate_market()
                    if self.stream is not None:
                        self.stream.close()
                return
            if data.get("symbol", event.get("symbol")) != SYMBOL:
                return
            if channel == "price":
                mark = D(data["mark_price"])
                if not mark.is_finite() or mark <= 0:
                    raise QuoteUnavailable("Invalid live mark")
                with self.market_lock:
                    self.live_mark = mark
                    self.mark_received_at = time.monotonic()
                self.guard_wake.set()
            elif channel == "depth_book":
                book = self.book_top(data)
                with self.market_lock:
                    self.live_book = book
                    self.book_received_at = time.monotonic()
                self.guard_wake.set()
            elif channel == "position":
                with self.market_lock:
                    self.guard_position = D(data["qty"])
                self.guard_wake.set()
                self.wake_event.set()
            elif channel == "order":
                cl_id = str(data.get("cl_ord_id", ""))
                if cl_id.startswith(PREFIX) and not data.get("reduce_only"):
                    with self.market_lock:
                        if str(data.get("status", "")).lower() in (
                                "filled", "canceled", "cancelled", "rejected"):
                            self.guard_entries.pop(cl_id, None)
                            self.cancel_requests.discard(cl_id)
                        elif cl_id in self.guard_entries:
                            self.guard_entries[cl_id].update(data)
                self.guard_wake.set()
                self.wake_event.set()
        except (QuoteUnavailable, ValueError, ArithmeticError, AttributeError, KeyError, TypeError):
            # Malformed market data must never leave old quotes marked fresh.
            self.invalidate_market()

    def request_entry_cancel(self, cl_id, reason):
        with self.market_lock:
            if cl_id in self.cancel_requests:
                return
            self.cancel_requests.add(cl_id)
        try:
            if not DRY_RUN:
                with self.entry_io_lock:
                    unwrap(self.client._post_signed(
                        "/api/cancel_order", {"cl_ord_id": cl_id}))
            self.log(f"Entry cancel requested ({reason}): {cl_id}")
            self.wake_event.set()
        except Exception:
            with self.market_lock:
                self.cancel_requests.discard(cl_id)
            raise

    def protect_entries_once(self):
        with self.market_lock:
            entries = list(self.guard_entries.items())
            position = self.guard_position
        if not entries:
            return
        try:
            mark, best_bid, best_ask = self.live_snapshot()
            reason = None
        except QuoteUnavailable:
            reason = "live market data unavailable"
        for cl_id, order in entries:
            why = reason
            if position != 0:
                why = "position detected"
            if why is None:
                side, price = order["side"], D(order["price"])
                distance = ((mark - price) if side == "buy" else
                            (price - mark)) / mark * 10000
                if distance <= MIN_BPS + EARLY_PULL_BPS:
                    why = "mark approached entry"
                elif distance > MAX_BPS:
                    why = "entry outside configured band"
                elif self.too_close_to_book(side, price, mark, best_bid, best_ask):
                    why = "opposite book approached entry"
            if why:
                self.request_entry_cancel(cl_id, why)

    def start_stream(self):
        if websocket is None:
            raise RuntimeError("websocket-client is required; install requirements.txt")

        def protect():
            while not self.guard_stop.is_set():
                self.guard_wake.wait(GUARD_CHECK_SECONDS)
                self.guard_wake.clear()
                try:
                    self.protect_entries_once()
                except Exception as exc:
                    self.log(f"Live guard cancellation failed: {exc}; stopping for cleanup")
                    self.stop_event.set()
                    self.wake_event.set()
                    return

        self.guard_thread = threading.Thread(target=protect, daemon=True)
        self.guard_thread.start()

        def public_backup():
            announced = False
            last_error_at = 0.0
            while not self.guard_stop.is_set():
                now = time.monotonic()
                retry_delay = MARKET_BACKUP_SECONDS
                with self.market_lock:
                    needs_refresh = (self.live_mark is None or self.live_book is None
                        or now - self.mark_received_at >= MARKET_BACKUP_SECONDS
                        or now - self.book_received_at >= MARKET_BACKUP_SECONDS)
                if needs_refresh:
                    try:
                        self.refresh_public_snapshot()
                        if not announced:
                            self.log("Fresh HTTP market backup ready; WebSocket monitoring remains active")
                            announced = True
                    except Exception as exc:
                        if getattr(getattr(exc, "response", None), "status_code", None) == 429:
                            retry_delay = 5.0
                        if time.monotonic() - last_error_at >= 10:
                            self.log(f"Market backup unavailable: {exc}; new entries blocked until fresh data")
                            last_error_at = time.monotonic()
                self.guard_stop.wait(retry_delay)

        self.market_backup_thread = threading.Thread(target=public_backup, daemon=True)
        self.market_backup_thread.start()

        def on_error(ws, error):
            self.invalidate_market()
            self.log(f"WebSocket connection error: {error}; fresh HTTP backup will retry")

        def on_close(ws, code, message):
            self.invalidate_market()
            if not self.stop_event.is_set():
                self.log(f"WebSocket disconnected (code {code}); reconnecting with HTTP backup")

        def on_open(ws):
            self.invalidate_market()
            self.log("WebSocket connected; subscribing to mark, depth, orders and positions")
            ws.send(json.dumps({"auth": {
                "token": self.client.token,
                "streams": [{"channel": "position"}, {"channel": "order"}],
            }}))
            for channel in ("price", "depth_book"):
                ws.send(json.dumps({"subscribe": {"channel": channel, "symbol": SYMBOL}}))

        def listen():
            while not self.stop_event.is_set() and not self.guard_stop.is_set():
                self.stream = websocket.WebSocketApp(
                    "wss://perps.standx.com/ws-stream/v1",
                    on_open=on_open,
                    on_message=lambda ws, raw: self.on_stream_message(raw),
                    on_close=on_close,
                    on_error=on_error,
                )
                try:
                    self.stream.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as exc:
                    self.log(f"Order stream interrupted: {exc}")
                self.invalidate_market()
                if self.stop_event.wait(2):
                    break

        threading.Thread(target=listen, daemon=True).start()

    def get_balance(self):
        data = unwrap(self.client._get("/api/query_balance", auth=True))
        if not isinstance(data, dict) or "cross_available" not in data:
            raise RuntimeError(f"Unexpected balance response: {data}")
        return data

    def get_mark(self):
        with self.market_lock:
            if (self.live_mark is not None
                    and time.monotonic() - self.mark_received_at <= MARKET_DATA_MAX_AGE):
                return self.live_mark
        return D(self.client.get_mark_price(SYMBOL))

    def get_position(self):
        data = unwrap(self.client._get(
            "/api/query_positions", {"symbol": SYMBOL}, auth=True
        ))
        rows = data if isinstance(data, list) else data.get("positions", [])
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected position response: {data}")
        for row in rows:
            if row.get("symbol") == SYMBOL:
                if "qty" not in row:
                    raise RuntimeError(f"Position quantity missing: {row}")
                qty = D(row["qty"])
                with self.market_lock:
                    self.guard_position = qty
                if qty:
                    self.guard_wake.set()
                return qty, D(row.get("entry_price", "0"))
        with self.market_lock:
            self.guard_position = Decimal(0)
        return Decimal(0), Decimal(0)

    def all_open_orders(self):
        data = unwrap(self.client._get(
            "/api/query_open_orders", {"symbol": SYMBOL, "limit": 1200}, auth=True
        ))
        rows = data if isinstance(data, list) else data.get("orders", [])
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected open orders response: {data}")
        orders = [x for x in rows if x.get("symbol") == SYMBOL]
        self.quote_snapshot = {
            x["side"]: D(x["price"]) for x in self.owned(orders)
            if not x.get("reduce_only") and x.get("side") in ("buy", "sell")
        }
        with self.market_lock:
            for order in self.owned(orders):
                cl_id = str(order.get("cl_ord_id", ""))
                if not order.get("reduce_only") and cl_id in self.active_entry_ids:
                    self.guard_entries[cl_id] = dict(order)
        self.guard_wake.set()
        return orders

    def publish_status(self):
        mark = self.get_mark()
        qty, entry = self.get_position()
        balance = self.get_balance()
        orders = self.all_open_orders()
        self.status({
            "mark": str(mark), "qty": str(qty), "entry": str(entry),
            "equity": str(balance["equity"]),
            "available": str(balance["cross_available"]),
            "orders": orders, "leverage": self.leverage,
            "entry_fill_count": self.entry_fill_count,
            "uptime_minutes": self.uptime_meter.observe(mark, qty, orders),
        })

    def order_state(self, cl_id):
        data = unwrap(self.client._get(
            "/api/query_order", {"cl_ord_id": cl_id}, auth=True
        ))
        if not isinstance(data, dict) or str(data.get("cl_ord_id")) != cl_id:
            raise RuntimeError(f"Order {cl_id} could not be verified")
        return data

    def record_entry_fill(self, cl_id, order):
        if cl_id not in self.filled_entry_ids and D(order.get("fill_qty", "0")) > 0:
            self.filled_entry_ids.add(cl_id)
            self.entry_fill_count += 1
            self.log(f"Entry filled: {cl_id} ({order.get('fill_qty')} {SYMBOL}); total: {self.entry_fill_count}")

    def reconcile_missing_entries(self, orders):
        seen = {str(x.get("cl_ord_id")) for x in self.owned(orders)
                if not x.get("reduce_only")}
        # A late cancellation from an older snapshot may outlive active_entry_ids.
        # Verify every pending cancellation by order ID before allowing new quotes.
        with self.market_lock:
            missing = (self.active_entry_ids | self.cancel_requests) - seen
        if not missing or DRY_RUN:
            return True
        states = {}
        for attempt in range(2):
            try:
                states = {cl_id: self.order_state(cl_id) for cl_id in missing}
            except (requests.RequestException, RuntimeError) as exc:
                self.log(f"Waiting for order confirmation: {exc}")
                return False
            if all(str(x.get("status", "")).lower() in
                   ("filled", "canceled", "cancelled", "rejected")
                   for x in states.values()):
                break
            if attempt < 1:
                time.sleep(0.2)
        else:
            self.log("Entry order is pending; waiting without sending a duplicate")
            return False
        for cl_id, state in states.items():
            self.record_entry_fill(cl_id, state)
            if state.get("status") == "rejected":
                self.log(f"Entry rejected: {state.get('remark', '')}")
        # Confirm that delayed fills have not created a position. The caller
        # handles any position before placing new maker quotes.
        if self.get_position()[0] != 0:
            return False
        time.sleep(0.25)
        if self.get_position()[0] != 0:
            return False
        if any(str(x.get("cl_ord_id")) in missing for x in self.all_open_orders()):
            self.log("Order list has not settled; waiting before new quotes")
            return False
        self.active_entry_ids.difference_update(missing)
        for cl_id in missing:
            self.pending_entry_cancels.pop(cl_id, None)
            with self.market_lock:
                self.guard_entries.pop(cl_id, None)
                self.cancel_requests.discard(cl_id)
        self.log("Entry orders settled and flat verified; maker quotes resume")
        return True

    def depth(self):
        with self.market_lock:
            if (self.live_book is not None
                    and time.monotonic() - self.book_received_at <= MARKET_DATA_MAX_AGE):
                return self.live_book
        data = unwrap(self.client._get(
            "/api/query_depth_book", {"symbol": SYMBOL}, auth=False
        ))
        return self.book_top(data)

    def too_close_to_book(self, side, price, mark, best_bid, best_ask):
        gap = best_ask - price if side == "buy" else price - best_bid
        return gap / mark * 10000 <= BOOK_GUARD_BPS

    def send(self, side, qty, price, reduce_only=False):
        cl_id = PREFIX + uuid.uuid4().hex[:20]
        payload = {
            "symbol": SYMBOL, "side": side, "order_type": "limit",
            "qty": format(qty, "f"), "price": format(price, "f"),
            "time_in_force": "alo", "reduce_only": reduce_only,
            "cl_ord_id": cl_id,
        }
        if self.stop_event.is_set():
            return None
        if DRY_RUN:
            self.log(f"[SIMULATION] new_order {payload}")
            return None
        if not reduce_only:
            # Recheck after waiting for other writes. The guard can cancel while
            # the main loop is fetching account data, but writes are serialized.
            with self.entry_io_lock:
                try:
                    fresh_mark, best_bid, best_ask = self.live_snapshot()
                except QuoteUnavailable as exc:
                    self.log(f"Entry skipped: {exc}")
                    return None
                with self.market_lock:
                    pending_cancel = bool(self.cancel_requests)
                    position = self.guard_position
                distance = ((fresh_mark - price) if side == "buy"
                            else (price - fresh_mark)) / fresh_mark * 10000
                if (self.stop_event.is_set() or pending_cancel or position != 0
                        or not MIN_BPS + EARLY_PULL_BPS < distance <= MAX_BPS
                        or self.too_close_to_book(side, price, fresh_mark,
                                                  best_bid, best_ask)):
                    self.log(f"Skipped unsafe or pending {side} entry; retrying")
                    return None
                self.entry_submitted = True
                self.active_entry_ids.add(cl_id)
                with self.market_lock:
                    self.guard_entries[cl_id] = dict(payload)
                self.log(f"Sending {side} entry: {qty} @ {price}")
                unwrap(self.client._post_signed("/api/new_order", payload))
            self.guard_wake.set()
        else:
            self.log(f"Sending {side} exit: {qty} @ {price}")
            unwrap(self.client._post_signed("/api/new_order", payload))
        # The exchange processes accepted orders asynchronously. Keep the ID
        # pending and send the opposite quote without a blocking confirmation
        # loop. Reconcile it before any future replacement or duplicate.
        return cl_id

    def cancel(self, order):
        if DRY_RUN:
            self.log(f"[SIMULATION] cancel {order.get('cl_ord_id')}")
            return
        cl_id = str(order.get("cl_ord_id", ""))
        if cl_id.startswith(PREFIX) and not order.get("reduce_only"):
            self.request_entry_cancel(cl_id, "quote replacement or cleanup")
        else:
            unwrap(self.client._post_signed(
                "/api/cancel_order", {"order_id": int(order["id"])}
            ))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(x.get("id") == order["id"] for x in self.all_open_orders()):
                self.reconcile_missing_entries(self.all_open_orders())
                return
            self.wake_event.wait(0.2)
            self.wake_event.clear()
        raise RuntimeError("Cancellation unconfirmed; bot stopped")

    def owned(self, orders):
        return [x for x in orders if str(x.get("cl_ord_id", "")).startswith(PREFIX)]

    def cancel_owned(self, orders):
        for order in self.owned(orders):
            self.cancel(order)

    def recover_one_sided(self, orders):
        """Rebuild both quotes when a lone entry remains on the book."""
        entries = [x for x in self.owned(orders) if not x.get("reduce_only")]
        sides = {x.get("side") for x in entries}
        if len(entries) != 1 or len(sides) != 1 or not sides <= {"buy", "sell"}:
            self.one_sided_since = None
            self.one_sided_side = None
            return orders
        side = entries[0]["side"]
        now = time.monotonic()
        if self.one_sided_side != side or self.one_sided_since is None:
            self.one_sided_side = side
            self.one_sided_since = now
            self.log(f"Only {side} entry visible; retrying the missing side")
            return orders
        if now - self.one_sided_since < ONE_SIDED_RESET_SECONDS:
            return orders
        # An accepted order can appear in the order list late. Never rebuild
        # while the unseen opposite order may still be live or filling.
        if not self.reconcile_missing_entries(orders):
            seen = {str(x.get("cl_ord_id")) for x in entries}
            for cl_id in self.active_entry_ids - seen:
                if now - self.pending_entry_cancels.get(cl_id, 0) < 10:
                    continue
                try:
                    state = self.order_state(cl_id)
                    if str(state.get("status", "")).lower() in (
                            "filled", "canceled", "cancelled", "rejected"):
                        continue
                    if not DRY_RUN:
                        unwrap(self.client._post_signed(
                            "/api/cancel_order", {"cl_ord_id": cl_id}))
                    self.pending_entry_cancels[cl_id] = now
                    self.log(f"Cancel requested for delayed entry {cl_id}; waiting for confirmation")
                except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                    self.log(f"Pending entry cancellation not confirmed: {exc}")
            self.log("One-sided reset waiting for pending entry confirmation")
            return orders
        if not DRY_RUN and self.get_position()[0] != 0:
            return orders
        self.log(f"One-sided for {ONE_SIDED_RESET_SECONDS}s; canceling {side} and rebuilding both quotes")
        self.cancel(entries[0])
        self.one_sided_since = None
        self.one_sided_side = None
        return self.all_open_orders() if not DRY_RUN else []

    def entry_prices(self, mark, book=None):
        best_bid, best_ask = book if book is not None else self.depth()
        tick = Decimal(1).scaleb(-self.price_decimals)
        # Do not cross the spread; ALO must rest on the book.
        bid = mark * (1 - TARGET_BPS / 10000)
        ask = mark * (1 + TARGET_BPS / 10000)
        bid = quantize_step(bid, self.price_decimals, ROUND_DOWN)
        ask = quantize_step(ask, self.price_decimals, ROUND_UP)
        if not (best_bid < best_ask and bid <= best_ask - tick
                and ask >= best_bid + tick):
            raise QuoteUnavailable("Unsafe entry price; mark and book moved apart")
        return bid, ask

    def entry_qty(self, mark):
        free = max(Decimal(0), D(self.get_balance()["cross_available"]))
        # Use the percentage and leverage chosen in the panel, split evenly
        # between the two sides. The exchange may still require fee reserves.
        per_side_notional = free * MAX_MARGIN_FRACTION * self.leverage / 2
        qty = quantize_step(per_side_notional / mark, self.qty_decimals)
        if qty < self.min_qty:
            raise RuntimeError(
                f"Calculated qty {qty} below minimum {self.min_qty}; "
                "no order sent."
            )
        return qty

    def entry_pair_qty(self, mark, owned):
        # A resting quote has already locked part of the pair's margin. Keep
        # its original common size instead of halving the remaining free balance.
        if not owned:
            self.pair_quote_qty = self.entry_qty(mark)
        elif getattr(self, "pair_quote_qty", None) is None:
            # Recover a common size from observed orders if no plan is cached.
            self.pair_quote_qty = min(D(order["qty"]) for order in owned)
        return self.pair_quote_qty

    def manage_entry(self, mark, orders):
        try:
            mark, best_bid, best_ask = self.live_snapshot()
        except QuoteUnavailable as exc:
            self.log(str(exc))
            return
        if not self.reconcile_missing_entries(orders):
            return
        with self.market_lock:
            pending_cancels = len(self.cancel_requests)
        if pending_cancels:
            now = time.monotonic()
            if now - getattr(self, "last_pending_cancel_log", 0.0) >= 5:
                self.log(f"Waiting for terminal confirmation of {pending_cancels} entry cancellation(s)")
                self.last_pending_cancel_log = now
            return
        if not DRY_RUN and self.get_position()[0] != 0:
            return
        owned = self.owned(orders)
        sides = {side: [x for x in owned if x.get("side") == side]
                 for side in ("buy", "sell")}
        if any(len(rows) > 1 for rows in sides.values()):
            raise RuntimeError("Duplicate bot entry orders; stopped")
        try:
            planned_qty = self.entry_pair_qty(mark, owned)
        except RuntimeError as exc:
            if "below minimum" not in str(exc):
                raise
            self.log(f"Waiting for available balance: {exc}")
            return
        for side in ("buy", "sell"):
            if self.stop_event.is_set():
                return
            if not DRY_RUN and self.get_position()[0] != 0:
                return
            try:
                mark, best_bid, best_ask = self.live_snapshot()
            except QuoteUnavailable as exc:
                self.log(f"Waiting for a valid order book: {exc}")
                return
            current = sides[side][0] if sides[side] else None
            if current is not None:
                distance = ((mark - D(current["price"])) if side == "buy"
                            else (D(current["price"]) - mark)) / mark * 10000
                current_book_safe = not self.too_close_to_book(
                    side, D(current["price"]), mark, best_bid, best_ask)
                if (MIN_BPS + EARLY_PULL_BPS < distance <= MAX_BPS
                        and current_book_safe
                        and D(current["qty"]) == planned_qty):
                    continue
                # Check the prospective quote while the current order is still
                # resting. Keep an eligible, safe quote when the new snapshot
                # is temporarily unusable instead of creating an uptime gap.
                try:
                    candidate_bid, candidate_ask = self.entry_prices(
                        mark, (best_bid, best_ask))
                    candidate = candidate_bid if side == "buy" else candidate_ask
                    candidate_safe = not self.too_close_to_book(
                        side, candidate, mark, best_bid, best_ask)
                except QuoteUnavailable:
                    candidate_safe = False
                if not candidate_safe:
                    self.cancel(current)
                    return
                self.cancel(current)
                if not DRY_RUN and self.get_position()[0] != 0:
                    return
            # Only fetch a new snapshot when an existing order was canceled.
            try:
                if current is not None:
                    mark, best_bid, best_ask = self.live_snapshot()
                book = (best_bid, best_ask)
                bid, ask = self.entry_prices(mark, book)
            except QuoteUnavailable as exc:
                self.log(f"Quote skipped, retrying next scan: {exc}")
                return
            target = bid if side == "buy" else ask
            best_bid, best_ask = book
            if self.too_close_to_book(side, target, mark, best_bid, best_ask):
                self.log(f"Waiting: {side} entry is too close to the order book")
                continue
            try:
                qty = planned_qty
            except RuntimeError as exc:
                if "below minimum" not in str(exc):
                    raise
                self.log(f"Waiting for available balance: {exc}")
                return
            submitted = self.send(side, qty, target)
            if submitted is None and self.active_entry_ids - {
                    str(x.get("cl_ord_id")) for x in self.all_open_orders()}:
                return
            # A newly placed entry could have filled before the second side.
            if not DRY_RUN and self.get_position()[0] != 0:
                return

    def close_filled_position(self):
        # First remove the opposite entry so it cannot fill during the exit.
        try:
            self.cancel_owned(self.all_open_orders())
        except Exception:
            if self.owned(self.all_open_orders()):
                raise
            # A fill can race with cancellation and remove the order itself.
        if DRY_RUN:
            self.log("[SIMULATION] reduce-only post-only limit exit")
            return
        current_qty, entry_price = self.get_position()
        if current_qty == 0:
            self.reconcile_missing_entries(self.all_open_orders())
            return
        if self.exit_submitted:
            raise RuntimeError("Exit already submitted; no duplicate sent")
        self.exit_submitted = True
        try:
            loss = (self.get_mark() - entry_price) * current_qty
            if entry_price and loss <= -self.start_equity * MAX_LOSS_FRACTION:
                self.log("Emergency loss threshold reached; using market exit")
            else:
                try:
                    self.try_maker_exit(current_qty)
                except QuoteUnavailable as exc:
                    self.log(f"Maker exit unavailable ({exc}); using emergency exit")
            if self.get_position()[0] != 0:
                self.market_exit_remaining()
            if self.get_position()[0] != 0:
                raise RuntimeError("Exit incomplete; check the position immediately")
            self.exit_submitted = False
            self.reconcile_missing_entries(self.all_open_orders())
            self.log("Position closed and verified; quoting resumes")
        except Exception:
            # The exit may have reached the exchange even if HTTP timed out.
            # Keep exit_submitted set so shutdown never submits it a second time.
            raise

    def try_maker_exit(self, position_qty):
        best_bid, best_ask = self.depth()
        tick = Decimal(1).scaleb(-self.price_decimals)
        side = "sell" if position_qty > 0 else "buy"
        if side == "sell":
            price = max(best_bid + tick, best_ask - tick)
            price = quantize_step(price, self.price_decimals, ROUND_UP)
        else:
            price = min(best_ask - tick, best_bid + tick)
            price = quantize_step(price, self.price_decimals, ROUND_DOWN)
        if not (best_bid < best_ask and
                (price > best_bid if side == "sell" else price < best_ask)):
            raise QuoteUnavailable("No safe post-only exit price")
        cl_id = PREFIX + uuid.uuid4().hex[:20]
        payload = {
            "symbol": SYMBOL, "side": side, "order_type": "limit",
            "time_in_force": "alo", "qty": format(abs(position_qty), "f"),
            "price": format(price, "f"), "reduce_only": True,
            "cl_ord_id": cl_id,
        }
        self.log(f"Maker exit submitted: {side} {payload['qty']} @ {price}")
        unwrap(self.client._post_signed("/api/new_order", payload))
        deadline = time.monotonic() + EXIT_MAKER_WAIT_SECONDS
        state = None
        while time.monotonic() < deadline and not self.stop_event.is_set():
            try:
                state = self.order_state(cl_id)
            except (requests.RequestException, RuntimeError):
                self.wake_event.wait(0.25)
                self.wake_event.clear()
                continue
            status = str(state.get("status", "")).lower()
            if status in ("filled", "canceled", "cancelled", "rejected"):
                break
            self.wake_event.wait(0.25)
            self.wake_event.clear()
        if state is None:
            raise RuntimeError("Maker exit unconfirmed; check StandX before another exit")
        status = str(state.get("status", "")).lower()
        if status not in ("filled", "canceled", "cancelled", "rejected"):
            self.log("Maker exit still resting; canceling before any fallback")
            unwrap(self.client._post_signed("/api/cancel_order", {"cl_ord_id": cl_id}))
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                state = self.order_state(cl_id)
                status = str(state.get("status", "")).lower()
                if status in ("filled", "canceled", "cancelled", "rejected"):
                    break
                self.wake_event.wait(0.25)
                self.wake_event.clear()
            else:
                raise RuntimeError("Maker exit cancellation unconfirmed; no market order sent")
        if any(str(x.get("cl_ord_id")) == cl_id for x in self.all_open_orders()):
            raise RuntimeError("Maker exit still appears open; no duplicate exit sent")
        self.log(f"Maker exit state: {status}, filled: {state.get('fill_qty', '0')}")
        # The position and the order can settle in different API snapshots.
        if status == "filled":
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.get_position()[0] == 0:
                    return
                self.wake_event.wait(0.25)
                self.wake_event.clear()
        else:
            self.wake_event.wait(0.25)
            self.wake_event.clear()

    def market_exit_remaining(self):
        current_qty, _ = self.get_position()
        if current_qty == 0:
            return
        payload = {
            "symbol": SYMBOL,
            "side": "sell" if current_qty > 0 else "buy",
            "order_type": "market",
            "time_in_force": "ioc",
            "qty": format(abs(current_qty), "f"),
            "reduce_only": True,
            "cl_ord_id": PREFIX + uuid.uuid4().hex[:20],
        }
        self.log(f"Maker exit did not flatten the position; market exit for remaining {current_qty}")
        unwrap(self.client._post_signed("/api/new_order", payload))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.wake_event.wait(0.25)
            self.wake_event.clear()
            if self.get_position()[0] == 0:
                try:
                    exit_state = self.order_state(payload["cl_ord_id"])
                except (requests.RequestException, RuntimeError):
                    continue
                if str(exit_state.get("status", "")).lower() not in ("filled", "canceled"):
                    continue
                if self.get_position()[0] != 0:
                    continue
                return
        raise RuntimeError("Exit unconfirmed or partial; bot stopped. Check StandX position immediately")

    def cancel_and_verify_on_stop(self):
        """Retry transport failures during cleanup; never resend a new order."""
        if DRY_RUN:
            self.log("Simulation stopped; no live orders were sent.")
            return self.get_position()
        for attempt in range(6):
            try:
                for _ in range(3):
                    owned = self.owned(self.all_open_orders())
                    if not owned:
                        break
                    for order in owned:
                        self.cancel(order)
                remaining = self.owned(self.all_open_orders())
                if remaining:
                    raise RuntimeError(f"{len(remaining)} bot orders still open")
                position = self.get_position()
                self.log("All bot-owned open orders canceled and verified.")
                return position
            except requests.RequestException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code is not None and code != 429 and code < 500:
                    raise
                if attempt == 5:
                    raise
                delay = min(2 ** attempt, 5)
                self.log(f"Cleanup connection failure; retry {attempt + 2}/6 in {delay}s. "
                         "New entries remain disabled; inspect StandX orders and position.")
                time.sleep(delay)

    def run(self):
        try:
            startup_orders = self.all_open_orders()
            if startup_orders:
                raise RuntimeError(
                    f"Existing {SYMBOL} orders detected. Review them in StandX before starting."
                )
            if self.get_position()[0] != 0:
                raise RuntimeError("Existing position detected; manage it in StandX before starting")
            self.start_stream()
            self.log(f"Mode={'SIMULATION' if DRY_RUN else 'LIVE'}, {SYMBOL}, "
                     f"entry={TARGET_BPS} bps, leverage={self.leverage}x")
            while not self.stop_event.is_set():
                qty, entry = self.get_position()
                if qty:
                    if not self.entry_submitted:
                        raise RuntimeError("Unrecognized position detected; no automatic exit")
                    self.close_filled_position()
                    continue
                mark = self.get_mark()
                orders = self.all_open_orders()
                balance = self.get_balance()
                self.status({
                    "mark": str(mark), "qty": str(qty), "entry": str(entry),
                    "equity": str(balance["equity"]),
                    "available": str(balance["cross_available"]),
                    "orders": orders, "leverage": self.leverage,
                    "entry_fill_count": self.entry_fill_count,
                    "uptime_minutes": self.uptime_meter.observe(mark, qty, orders),
                })
                unknown = [x for x in orders if x not in self.owned(orders)]
                if unknown:
                    raise RuntimeError("Unknown order appeared; stopped")
                exits = [x for x in self.owned(orders) if x.get("reduce_only")]
                if exits:
                    self.cancel_owned(exits)
                    orders = self.all_open_orders() if not DRY_RUN else []
                if not self.stop_event.is_set():
                    orders = self.recover_one_sided(orders)
                    self.manage_entry(mark, orders)
                    self.publish_status()
                    if not DRY_RUN and self.get_position()[0] != 0:
                        continue
                with self.market_lock:
                    pending_cancel = bool(self.cancel_requests)
                self.wake_event.wait(0.25 if pending_cancel else LOOP_SECONDS)
                self.wake_event.clear()
        except Exception as exc:
            self.log(f"STOPPED: {exc}")
        finally:
            self.guard_stop.set()
            self.guard_wake.set()
            if self.guard_thread is not None:
                self.guard_thread.join(timeout=10)
            if self.market_backup_thread is not None:
                self.market_backup_thread.join(timeout=10)
            if self.stream is not None:
                self.stream.close()
            # Retry cancel/query transport failures only. New-order and exit
            # submissions stay outside the retry loop to prevent duplicate exits.
            try:
                position_qty, _ = self.cancel_and_verify_on_stop()
                if position_qty and self.entry_submitted and not self.exit_submitted:
                    self.log("Position during shutdown; attempting maker-first reduce-only exit")
                    self.close_filled_position()
                elif position_qty:
                    self.log(f"POSITION STILL OPEN ({position_qty} {SYMBOL}); check StandX immediately")
            except Exception as exc:
                self.log(f"CANCEL NOT CONFIRMED: {exc}. Check orders and positions in StandX immediately.")
            try:
                self.publish_status()
            except Exception as exc:
                self.log(f"Final account status unavailable: {exc}")


class Dashboard:
    MARKETS = (
        "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "HYPE-USD",
        "XAU-USD", "XAG-USD", "CL-USD", "TSLA-USD", "SPCX-USD", "MU-USD",
    )
    TEXT = {
        "EN": {
            "subtitle": "Two-sided market making dashboard", "settings": "STRATEGY SETTINGS",
            "credit": "Made by @crryptooKerim",
            "market": "Market", "target": "Target BPS", "lower": "Minimum BPS",
            "upper": "Maximum BPS", "margin": "Balance usage %", "leverage": "Leverage",
            "loss": "Emergency loss threshold %", "live": "Enable live orders",
            "start": "Start bot", "stop": "Stop and cancel orders",
            "orders": "Open orders", "logs": "Activity log", "side": "Side",
            "counts": "Open: {open}    Fills: {filled}    Est. two-sided / last 60m: {minutes:.1f}m (goals 30 / 42m)",
            "price": "Price", "qty": "Quantity", "type": "Type", "status": "Status",
            "entry": "Entry", "exit": "Exit", "ready": "Ready", "loading": "Loading market data…",
            "connecting": "Connecting…", "running": "Running", "simulation": "Simulation",
            "stopping": "Stopping and canceling orders…", "stopped": "Stopped",
            "market_hint": "{symbol} · max {maximum}x · trading",
            "market_error": "Market info unavailable: {error}",
            "invalid": "Invalid settings", "market_title": "Market data",
            "market_wait": "Wait for market limits to load.",
            "bps_error": "BPS must satisfy 0 < min ≤ target ≤ max < 10.",
            "margin_error": "Balance usage must be above 0% and at most 100%.",
            "loss_error": "Emergency loss threshold must be above 0% and at most 5%.",
            "leverage_error": "Leverage exceeds this market's current limit.",
            "live_title": "Live trading",
            "live_confirm": "{symbol} · {target} BPS · balance {margin}% · leverage {leverage}x · loss threshold {loss}%\n\nFilled positions try a post-only reduce-only limit exit first. If it does not fill in 5 seconds or the loss threshold is reached, the remaining position uses a market exit with possible taker fees and slippage. Send real orders?",
            "closed": "The window will close after bot orders are canceled and checked.",
            "position": "Position", "available": "Available", "leverage_word": "Leverage",
            "startup_error": "Could not start", "market_disabled": "Market is not trading",
            "invalid_leverage": "Invalid leverage limit",
            "leverage_busy": "Review open orders and positions before changing leverage.",
            "setting_leverage": "Setting leverage", "leverage_unconfirmed": "Leverage change not confirmed; no orders sent.",
        },
        "TR": {
            "subtitle": "İki taraflı piyasa yapıcı paneli", "settings": "STRATEJİ AYARLARI",
            "credit": "@crryptooKerim tarafından yapıldı",
            "market": "Pazar", "target": "Hedef BPS", "lower": "Alt BPS",
            "upper": "Üst BPS", "margin": "Bakiye kullanımı %", "leverage": "Kaldıraç",
            "loss": "Acil zarar eşiği %", "live": "Canlı emirleri etkinleştir",
            "start": "Botu başlat", "stop": "Durdur ve emirleri iptal et",
            "orders": "Açık emirler", "logs": "İşlem günlüğü", "side": "Yön",
            "counts": "Açık: {open}    Dolum: {filled}    Tahmini çift taraf / son 60 dk: {minutes:.1f} dk (hedef 30 / 42)",
            "price": "Fiyat", "qty": "Miktar", "type": "Tür", "status": "Durum",
            "entry": "Giriş", "exit": "Çıkış", "ready": "Hazır", "loading": "Pazar bilgisi yükleniyor…",
            "connecting": "Bağlanıyor…", "running": "Çalışıyor", "simulation": "Simülasyon",
            "stopping": "Durduruluyor ve emirler iptal ediliyor…", "stopped": "Durdu",
            "market_hint": "{symbol} · en fazla {maximum}x · aktif",
            "market_error": "Pazar bilgisi alınamadı: {error}",
            "invalid": "Geçersiz ayar", "market_title": "Pazar bilgisi",
            "market_wait": "Pazar limitlerinin yüklenmesini bekleyin.",
            "bps_error": "BPS: 0 < alt ≤ hedef ≤ üst < 10 olmalı.",
            "margin_error": "Bakiye kullanımı %0'dan büyük ve en fazla %100 olmalı.",
            "loss_error": "Acil zarar eşiği %0'dan büyük ve en fazla %5 olmalı.",
            "leverage_error": "Kaldıraç pazarın güncel sınırını aşamaz.",
            "live_title": "Canlı işlem",
            "live_confirm": "{symbol} · {target} BPS · bakiye %{margin} · kaldıraç {leverage}x · zarar eşiği %{loss}\n\nDolan pozisyon önce post-only, pozisyon azaltıcı limit emirle kapatılmaya çalışılır. 5 saniyede dolmazsa veya zarar eşiğine ulaşırsa kalan pozisyon piyasa emriyle kapatılır; taker ücreti ve kayma oluşabilir. Gerçek emir gönderilsin mi?",
            "closed": "Emir iptali ve kontrolü bitince pencere kapanacak.",
            "position": "Pozisyon", "available": "Kullanılabilir", "leverage_word": "Kaldıraç",
            "startup_error": "Başlatılamadı", "market_disabled": "Pazar aktif değil",
            "invalid_leverage": "Geçersiz kaldıraç limiti",
            "leverage_busy": "Kaldıraç değişimi öncesi açık emir ve pozisyonları kontrol edin.",
            "setting_leverage": "Kaldıraç ayarlanıyor", "leverage_unconfirmed": "Kaldıraç değişikliği doğrulanamadı; emir gönderilmedi.",
        },
    }

    def __init__(self):
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "Standgrox.MakerBot.Desktop.1"
                )
            except (AttributeError, OSError):
                pass
        self.root = tk.Tk()
        self.root.title("Standgrox Maker Bot")
        self.root.geometry("1100x790")
        self.root.minsize(940, 700)
        self.bg, self.card, self.fg = "#050b08", "#0c1b14", "#f4fff7"
        self.muted, self.accent = "#b6d8bf", "#379b61"
        self.root.configure(bg=self.bg)
        self.setup_style()
        try:
            self.app_icon = tk.PhotoImage(file=str(bundled_asset("icon.png")))
            self.root.iconphoto(True, self.app_icon)
        except tk.TclError:
            pass
        if sys.platform == "win32":
            try:
                self.root.iconbitmap(str(bundled_asset("icon.ico")))
            except tk.TclError:
                pass
        self.events = queue.Queue()
        self.worker = None
        self.bot = None
        self.stop_event = threading.Event()
        self.closing = False
        self.last_stop_reason = None
        self.language = tk.StringVar(value="EN")
        self.market = tk.StringVar(value="BTC-USD")
        self.target = tk.StringVar(value="5.5")
        self.lower = tk.StringVar(value="5")
        self.upper = tk.StringVar(value="6")
        self.margin = tk.StringVar(value="10")
        self.leverage_limit = tk.StringVar(value="—")
        self.market_limit = None
        self.loss_limit = tk.StringVar(value="2")
        self.live = tk.BooleanVar(value=False)
        self.state = tk.StringVar()
        self.market_data = tk.StringVar(value="Mark: —    Leverage: —    Position: —")
        self.account_data = tk.StringVar(value="Equity: —    Available: —")
        self.order_counts = tk.StringVar(value="")
        self.last_open_count = 0
        self.last_fill_count = 0
        self.last_uptime_minutes = 0.0
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 20))
        self.logo = tk.PhotoImage(file=str(bundled_asset("icon.png"))).subsample(5, 5)
        ttk.Label(header, image=self.logo).pack(side="left", padx=(0, 16))
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text="STANDGROX MAKER BOT", style="Hero.TLabel").pack(anchor="w")
        self.subtitle = ttk.Label(titles, style="Muted.TLabel")
        self.subtitle.pack(anchor="w")
        upper_right = ttk.Frame(header)
        upper_right.pack(side="right", anchor="n")
        ttk.Combobox(upper_right, textvariable=self.language, values=("EN", "TR"),
                     state="readonly", width=5).pack(anchor="e")
        self.credit = ttk.Label(upper_right, style="Credit.TLabel", cursor="hand2")
        self.credit.pack(anchor="e", pady=(8, 0))
        self.credit.bind("<Button-1>", lambda _event: webbrowser.open(
            "https://x.com/crryptooKerim"))
        self.settings_frame = ttk.LabelFrame(outer, padding=15)
        self.settings_frame.pack(fill="x")
        fields = [("market", self.market), ("target", self.target),
                  ("lower", self.lower), ("upper", self.upper),
                  ("margin", self.margin), ("leverage", self.leverage_limit),
                  ("loss", self.loss_limit)]
        self.field_labels, self.inputs = [], []
        for i, (key, variable) in enumerate(fields):
            row, col = divmod(i, 4)
            label = ttk.Label(self.settings_frame)
            label.grid(row=row * 2, column=col, sticky="w", padx=9, pady=(4, 0))
            self.field_labels.append((label, key))
            if i in (0, 5):
                values = self.MARKETS if i == 0 else ()
                widget = ttk.Combobox(self.settings_frame, textvariable=variable,
                                      values=values, state="readonly", width=18)
            else:
                widget = ttk.Entry(self.settings_frame, textvariable=variable, width=18)
            widget.grid(row=row * 2 + 1, column=col, sticky="ew", padx=9, pady=(3, 12))
            self.inputs.append(widget)
        for col in range(4):
            self.settings_frame.columnconfigure(col, weight=1)
        self.market_hint = tk.StringVar()
        ttk.Label(self.settings_frame, textvariable=self.market_hint,
                  style="Accent.TLabel").grid(row=4, column=0, columnspan=4,
                                                sticky="w", padx=9, pady=(3, 0))
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=16)
        self.live_check = ttk.Checkbutton(controls, variable=self.live)
        self.live_check.pack(side="left")
        self.start_button = ttk.Button(controls, command=self.start, style="Action.TButton")
        self.start_button.pack(side="right", padx=(10, 0))
        self.stop_button = ttk.Button(controls, command=self.stop, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Label(outer, textvariable=self.state, style="Status.TLabel").pack(anchor="w")
        ttk.Label(outer, textvariable=self.market_data, style="Metric.TLabel").pack(anchor="w", pady=(9, 0))
        ttk.Label(outer, textvariable=self.account_data, style="Metric.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(outer, textvariable=self.order_counts, style="Metric.TLabel").pack(anchor="w")
        self.order_frame = ttk.LabelFrame(outer, padding=9)
        self.order_frame.pack(fill="both", expand=True, pady=(12, 9))
        self.table = ttk.Treeview(self.order_frame,
                                 columns=("side", "price", "qty", "type", "status"),
                                 show="headings", height=6)
        for name, width in (("side", 80), ("price", 145), ("qty", 125),
                            ("type", 120), ("status", 120)):
            self.table.column(name, width=width, anchor="center")
        self.table.pack(fill="both", expand=True)
        self.log_frame = ttk.LabelFrame(outer, padding=9)
        self.log_frame.pack(fill="both", expand=True)
        self.log_box = tk.Text(self.log_frame, height=7, state="disabled", wrap="word",
                               bg="#08150e", fg=self.fg, insertbackground=self.fg,
                               relief="flat", font=("Consolas", 11), padx=10, pady=8)
        self.log_box.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.language.trace_add("write", lambda *_: self.translate())
        self.market.trace_add("write", lambda *_: self.load_market_info())
        self.translate()
        self.load_market_info()
        self.root.after(200, self.poll)

    def t(self, key, **values):
        return self.TEXT[self.language.get()][key].format(**values)

    def translate(self):
        self.subtitle.configure(text=self.t("subtitle"))
        self.credit.configure(text=self.t("credit"))
        self.settings_frame.configure(text="  " + self.t("settings") + "  ")
        for label, key in self.field_labels:
            label.configure(text=self.t(key))
        self.live_check.configure(text=self.t("live"))
        self.start_button.configure(text=self.t("start"))
        self.stop_button.configure(text=self.t("stop"))
        self.order_frame.configure(text=self.t("orders"))
        self.log_frame.configure(text=self.t("logs"))
        self.order_counts.set(self.t("counts", open=self.last_open_count,
                                     filled=self.last_fill_count,
                                     minutes=self.last_uptime_minutes))
        for key in ("side", "price", "qty", "type", "status"):
            self.table.heading(key, text=self.t(key))
        if not self.worker or not self.worker.is_alive():
            self.state.set(self.t("ready"))
        if self.market_limit:
            self.market_hint.set(self.t("market_hint", symbol=self.market.get(),
                                        maximum=self.market_limit))
        else:
            self.market_hint.set(self.t("loading"))

    def setup_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=self.bg)
        style.configure("TLabel", background=self.bg, foreground=self.fg,
                        font=("Segoe UI", 11))
        style.configure("Hero.TLabel", font=("Segoe UI", 21, "bold"),
                        foreground=self.accent)
        style.configure("Muted.TLabel", foreground=self.muted)
        style.configure("Credit.TLabel", foreground="#a4cbb0",
                        font=("Segoe UI", 10, "underline"))
        style.configure("Accent.TLabel", background=self.card, foreground=self.accent,
                        font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 13, "bold"),
                        foreground=self.accent)
        style.configure("Metric.TLabel", font=("Segoe UI", 12), foreground=self.fg)
        style.configure("TLabelframe", background=self.card, bordercolor="#287a47",
                        borderwidth=2)
        style.configure("TLabelframe.Label", background=self.card,
                        foreground=self.accent, font=("Segoe UI", 12, "bold"))
        style.configure("TEntry", fieldbackground="#dceee0", foreground="#07120b",
                        insertcolor="#07120b", padding=9, font=("Segoe UI", 12))
        style.map("TEntry", fieldbackground=[("disabled", "#dceee0")],
                  foreground=[("disabled", "#07120b")])
        style.configure("TCombobox", fieldbackground="#dceee0", foreground="#07120b",
                        selectbackground="#b7dec2", selectforeground="#07120b",
                        background="#dceee0", arrowcolor="#07120b", padding=9,
                        font=("Segoe UI", 12))
        style.map("TCombobox", fieldbackground=[("readonly", "#dceee0"),
                                                 ("disabled", "#dceee0")],
                  foreground=[("readonly", "#07120b"), ("disabled", "#07120b")])
        self.root.option_add("*TCombobox*Listbox.background", "#dceee0")
        self.root.option_add("*TCombobox*Listbox.foreground", "#07120b")
        style.configure("TCheckbutton", background=self.bg, foreground=self.fg,
                        font=("Segoe UI", 11))
        style.map("TCheckbutton", background=[("active", self.bg)],
                  foreground=[("active", self.accent)])
        style.configure("TButton", padding=9, background="#15452a", foreground=self.fg,
                        font=("Segoe UI", 11, "bold"))
        style.configure("Action.TButton", background="#17643a", foreground="#ffffff")
        style.map("TButton", background=[("active", "#37c872"),
                                           ("disabled", "#2b4032")],
                  foreground=[("disabled", "#c4d7ca")])
        style.map("Action.TButton", background=[("active", "#227e49"),
                                                  ("disabled", "#294633")],
                  foreground=[("active", "#ffffff"), ("disabled", "#e6f0e8")])
        style.configure("Treeview", background="#0b2013", fieldbackground="#0b2013",
                        foreground=self.fg, rowheight=31, borderwidth=0,
                        font=("Segoe UI", 11))
        style.configure("Treeview.Heading", background="#205c36", foreground="#ffffff",
                        font=("Segoe UI", 11, "bold"))
        style.map("Treeview", background=[("selected", "#277a46")])

    def load_market_info(self):
        symbol = self.market.get()
        self.market_limit = None
        self.market_hint.set(self.t("loading"))
        self.start_button.configure(state="disabled")
        threading.Thread(target=self.fetch_market_info, args=(symbol,), daemon=True).start()

    def fetch_market_info(self, symbol):
        try:
            response = requests.get("https://perps.standx.com/api/query_symbol_info",
                                    params={"symbol": symbol}, timeout=10)
            response.raise_for_status()
            data = unwrap(response.json())
            info = next(x for x in data if x.get("symbol") == symbol) if isinstance(data, list) else data
            if info.get("status") != "trading" or info.get("enabled") is False:
                raise ValueError(f"{self.t('market_disabled')}: {info.get('status')}")
            maximum = int(info["max_leverage"])
            if maximum < 1:
                raise ValueError(self.t("invalid_leverage"))
            self.events.put(("market_info", (symbol, maximum)))
        except Exception as exc:
            self.events.put(("market_error", (symbol, str(exc))))

    def settings(self):
        target, lower, upper = (Decimal(x.get().strip())
                                for x in (self.target, self.lower, self.upper))
        margin = Decimal(self.margin.get().strip())
        loss = Decimal(self.loss_limit.get().strip())
        max_leverage = int(self.leverage_limit.get().strip())
        if not (Decimal(0) < lower <= target <= upper < Decimal(10)):
            raise ValueError(self.t("bps_error"))
        if not (Decimal(0) < margin <= Decimal(100)):
            raise ValueError(self.t("margin_error"))
        if not (Decimal(0) < loss <= Decimal(5)):
            raise ValueError(self.t("loss_error"))
        if self.market_limit is None or not 1 <= max_leverage <= self.market_limit:
            raise ValueError(self.t("leverage_error"))
        return target, lower, upper, margin / 100, loss / 100, max_leverage

    def start(self):
        global SYMBOL, TARGET_BPS, MIN_BPS, MAX_BPS, MAX_MARGIN_FRACTION
        global EARLY_PULL_BPS, MAX_LOSS_FRACTION, MAX_LEVERAGE, DRY_RUN, PREFIX
        if self.worker and self.worker.is_alive():
            return
        if self.market_limit is None:
            messagebox.showerror(self.t("market_title"), self.t("market_wait"))
            return
        try:
            (TARGET_BPS, MIN_BPS, MAX_BPS, MAX_MARGIN_FRACTION,
             MAX_LOSS_FRACTION, MAX_LEVERAGE) = self.settings()
            EARLY_PULL_BPS = min(Decimal("0.5"), (TARGET_BPS - MIN_BPS) / 2)
        except (ValueError, ArithmeticError) as exc:
            messagebox.showerror(self.t("invalid"), str(exc))
            return
        SYMBOL = self.market.get()
        DRY_RUN = not self.live.get()
        PREFIX = "SM-" + SYMBOL.replace("-", "") + "-"
        if not DRY_RUN:
            details = self.t("live_confirm", symbol=SYMBOL, target=TARGET_BPS,
                             margin=MAX_MARGIN_FRACTION * 100, leverage=MAX_LEVERAGE,
                             loss=MAX_LOSS_FRACTION * 100)
            if not messagebox.askyesno(self.t("live_title"), details):
                return
        self.stop_event = threading.Event()
        self.last_stop_reason = None
        self.last_open_count = 0
        self.last_fill_count = 0
        self.last_uptime_minutes = 0.0
        self.order_counts.set(self.t("counts", open=0, filled=0, minutes=0.0))
        self.state.set(self.t("connecting"))
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.live_check.configure(state="disabled")
        for widget in self.inputs:
            widget.configure(state="disabled")
        self.worker = threading.Thread(target=self.work, daemon=True)
        self.worker.start()

    def work(self):
        try:
            client = StandXClient()
            configured = unwrap(client._get(
                "/api/query_position_config", {"symbol": SYMBOL}, auth=True))
            if isinstance(configured, list):
                configured = configured[0]
            current_leverage = int(configured["leverage"])
            if current_leverage != MAX_LEVERAGE and not DRY_RUN:
                existing = unwrap(client._get(
                    "/api/query_open_orders", {"symbol": SYMBOL, "limit": 1200}, auth=True))
                open_orders = existing if isinstance(existing, list) else existing.get("orders", [])
                positions = unwrap(client._get(
                    "/api/query_positions", {"symbol": SYMBOL}, auth=True))
                position_rows = positions if isinstance(positions, list) else positions.get("positions", [])
                if open_orders or any(Decimal(str(p.get("qty", "0"))) != 0
                                      for p in position_rows if p.get("symbol") == SYMBOL):
                    raise RuntimeError(self.t("leverage_busy"))
                self.events.put(("log", f"{self.t('setting_leverage')}: {current_leverage}x → {MAX_LEVERAGE}x"))
                unwrap(client._post_signed(
                    "/api/change_leverage", {"symbol": SYMBOL, "leverage": MAX_LEVERAGE}))
                confirmed = False
                for _ in range(10):
                    if self.stop_event.wait(0.5):
                        return
                    check = unwrap(client._get(
                        "/api/query_position_config", {"symbol": SYMBOL}, auth=True))
                    if isinstance(check, list):
                        check = check[0]
                    if int(check["leverage"]) == MAX_LEVERAGE:
                        confirmed = True
                        break
                if not confirmed:
                    raise RuntimeError(self.t("leverage_unconfirmed"))
            if self.stop_event.is_set():
                return
            bot = MakerBot(log=lambda msg: self.events.put(("log", str(msg))),
                           status=lambda data: self.events.put(("status", data)),
                           stop_event=self.stop_event)
            self.bot = bot
            self.events.put(("state", "running" if not DRY_RUN else "simulation"))
            bot.run()
        except Exception as exc:
            self.events.put(("log", f"{self.t('startup_error')}: {exc}"))
        finally:
            self.bot = None
            self.events.put(("finished", None))

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.state.set(self.t("stopping"))
            self.stop_button.configure(state="disabled")
            self.stop_event.set()
            if self.bot is not None:
                self.bot.wake_event.set()

    def close(self):
        if self.worker and self.worker.is_alive():
            if not self.closing:
                self.closing = True
                self.stop()
                self.write_log(self.t("closed"))
            return
        self.root.destroy()

    def write_log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{time.strftime('%H:%M:%S')}  {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.write_log(payload)
                    if payload.startswith(("STOPPED:", f"{self.t('startup_error')}:")):
                        self.last_stop_reason = payload
                elif kind == "state":
                    self.state.set(self.t(payload))
                elif kind == "status":
                    self.last_open_count = len(payload["orders"])
                    self.last_fill_count = payload.get("entry_fill_count", 0)
                    self.last_uptime_minutes = payload.get("uptime_minutes", 0.0)
                    self.order_counts.set(self.t("counts", open=self.last_open_count,
                                                 filled=self.last_fill_count,
                                                 minutes=self.last_uptime_minutes))
                    self.market_data.set(
                        f"Mark: {payload['mark']}    {self.t('leverage_word')}: {payload['leverage']}x    "
                        f"{self.t('position')}: {payload['qty']} ({self.t('entry')} {payload['entry']})")
                    self.account_data.set(
                        f"Equity: {payload['equity']} DUSD    "
                        f"{self.t('available')}: {payload['available']} DUSD")
                    self.table.delete(*self.table.get_children())
                    for order in payload["orders"]:
                        self.table.insert("", "end", values=(
                            order.get("side"), order.get("price"), order.get("qty"),
                            self.t("exit") if order.get("reduce_only") else self.t("entry"),
                            order.get("status")))
                elif kind == "finished":
                    self.state.set(self.last_stop_reason or self.t("stopped"))
                    self.start_button.configure(state="normal" if self.market_limit else "disabled")
                    self.stop_button.configure(state="disabled")
                    self.live_check.configure(state="normal")
                    for i, widget in enumerate(self.inputs):
                        widget.configure(state="readonly" if i in (0, 5) else "normal")
                    if self.closing:
                        self.root.destroy()
                        return
                elif kind == "market_info":
                    symbol, maximum = payload
                    if symbol == self.market.get():
                        self.market_limit = maximum
                        self.leverage_limit.set(str(maximum))
                        self.inputs[5].configure(values=tuple(range(1, maximum + 1)))
                        self.market_hint.set(self.t("market_hint", symbol=symbol, maximum=maximum))
                        if not self.worker or not self.worker.is_alive():
                            self.start_button.configure(state="normal")
                elif kind == "market_error":
                    symbol, error = payload
                    if symbol == self.market.get():
                        self.market_hint.set(self.t("market_error", error=error))
        except queue.Empty:
            pass
        self.root.after(200, self.poll)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Dashboard().run()
