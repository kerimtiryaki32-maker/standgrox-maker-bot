"""StandX single-market maker bot with a local desktop dashboard.

Requires the existing standx_client.py and its .env credentials.
Run with: py main.py
"""

import time
import uuid
import os
import sys
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
import webbrowser

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
LOOP_SECONDS = 1
MAX_MARGIN_FRACTION = Decimal("0.10")  # at most 10% of free cross balance
MAX_LEVERAGE = 10                       # refuse to run if account is above this
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


class MakerBot:
    def __init__(self, log=print, status=None, stop_event=None):
        self.log = log
        self.status = status or (lambda data: None)
        self.stop_event = stop_event or threading.Event()
        self.client = StandXClient()
        self.info = self.get_info()
        self.price_decimals = int(self.info["price_tick_decimals"])
        self.qty_decimals = int(self.info["qty_tick_decimals"])
        self.min_qty = D(self.info["min_order_qty"])
        self.entry_submitted = False
        self.exit_submitted = False
        self.active_entry_ids = set()
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

    def get_balance(self):
        data = unwrap(self.client._get("/api/query_balance", auth=True))
        if not isinstance(data, dict) or "cross_available" not in data:
            raise RuntimeError(f"Unexpected balance response: {data}")
        return data

    def get_mark(self):
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
                return D(row.get("qty", "0")), D(row.get("entry_price", "0"))
        return Decimal(0), Decimal(0)

    def all_open_orders(self):
        data = unwrap(self.client._get(
            "/api/query_open_orders", {"symbol": SYMBOL, "limit": 1200}, auth=True
        ))
        rows = data if isinstance(data, list) else data.get("orders", [])
        if not isinstance(rows, list):
            raise RuntimeError(f"Unexpected open orders response: {data}")
        return [x for x in rows if x.get("symbol") == SYMBOL]

    def depth(self):
        data = unwrap(self.client._get(
            "/api/query_depth_book", {"symbol": SYMBOL}, auth=False
        ))
        bids = [D(level[0]) for level in data["bids"] if D(level[1]) > 0]
        asks = [D(level[0]) for level in data["asks"] if D(level[1]) > 0]
        if not bids or not asks:
            raise RuntimeError("Empty order book")
        best_bid, best_ask = max(bids), min(asks)
        if best_bid >= best_ask:
            raise RuntimeError("Crossed or stale order book")
        return best_bid, best_ask

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
            # A timeout can still mean the exchange accepted the order.
            self.entry_submitted = True
        self.log(f"Sending {side} {'exit' if reduce_only else 'entry'}: {qty} @ {price}")
        unwrap(self.client._post_signed("/api/new_order", payload))
        # HTTP success only accepts the request; verify actual book placement.
        for _ in range(8):
            if self.stop_event.wait(0.25):
                break
            if any(x.get("cl_ord_id") == cl_id for x in self.all_open_orders()):
                return cl_id
            if not reduce_only and self.get_position()[0]:
                # A fill before the first order-book snapshot is a valid outcome.
                return cl_id
        raise RuntimeError(
            f"Order {cl_id} not confirmed open. Inspect order history; "
            "bot stopped to prevent duplicate orders."
        )

    def cancel(self, order):
        if DRY_RUN:
            self.log(f"[SIMULATION] cancel {order.get('cl_ord_id')}")
            return
        unwrap(self.client._post_signed(
            "/api/cancel_order", {"order_id": int(order["id"])}
        ))
        for _ in range(16):
            time.sleep(0.25)
            if not any(x.get("id") == order["id"] for x in self.all_open_orders()):
                self.active_entry_ids.discard(str(order.get("cl_ord_id", "")))
                return
        raise RuntimeError("Cancellation unconfirmed; bot stopped")

    def owned(self, orders):
        return [x for x in orders if str(x.get("cl_ord_id", "")).startswith(PREFIX)]

    def cancel_owned(self, orders):
        for order in self.owned(orders):
            self.cancel(order)

    def entry_prices(self, mark):
        best_bid, best_ask = self.depth()
        tick = Decimal(1).scaleb(-self.price_decimals)
        # Do not cross the spread; ALO must rest on the book.
        bid = mark * (1 - TARGET_BPS / 10000)
        ask = mark * (1 + TARGET_BPS / 10000)
        bid = quantize_step(bid, self.price_decimals, ROUND_DOWN)
        ask = quantize_step(ask, self.price_decimals, ROUND_UP)
        if not (best_bid < best_ask and bid <= best_ask - tick
                and ask >= best_bid + tick):
            raise RuntimeError("Unsafe entry price")
        return bid, ask

    def entry_qty(self, mark):
        free = max(Decimal(0), D(self.get_balance()["cross_available"]))
        # Budget for both sides combined; never use the old hardcoded $21/side.
        # Keep a small margin/fee buffer even when the panel requests 100%.
        per_side_notional = free * MAX_MARGIN_FRACTION * Decimal("0.98") * self.leverage / 2
        qty = quantize_step(per_side_notional / mark, self.qty_decimals)
        if qty < self.min_qty:
            raise RuntimeError(
                f"Calculated qty {qty} below minimum {self.min_qty}; "
                "no order sent."
            )
        return qty

    def manage_entry(self, mark, orders):
        bid, ask = self.entry_prices(mark)
        owned = self.owned(orders)
        seen = {str(x.get("cl_ord_id")) for x in owned if not x.get("reduce_only")}
        missing = self.active_entry_ids - seen
        if missing and not DRY_RUN:
            raise RuntimeError("A bot entry disappeared from open orders; possible fill. Stopped without placing another entry")
        sides = {side: [x for x in owned if x.get("side") == side]
                 for side in ("buy", "sell")}
        if any(len(rows) > 1 for rows in sides.values()):
            raise RuntimeError("Duplicate bot entry orders; stopped")
        planned_qty = self.entry_qty(mark) if not owned else None
        for side, target in (("buy", bid), ("sell", ask)):
            if self.stop_event.is_set():
                return
            if not DRY_RUN and self.get_position()[0]:
                return
            current = sides[side][0] if sides[side] else None
            if current is not None:
                distance = ((mark - D(current["price"])) if side == "buy"
                            else (D(current["price"]) - mark)) / mark * 10000
                # Pull the approaching side before it reaches the minimum.
                approach_trigger = MIN_BPS + (TARGET_BPS - MIN_BPS) / 2
                if approach_trigger < distance <= MAX_BPS:
                    continue
                self.cancel(current)
                if not DRY_RUN and self.get_position()[0]:
                    return
            qty = planned_qty if planned_qty is not None else self.entry_qty(mark)
            if not DRY_RUN:
                latest_mark = self.get_mark()
                if abs(latest_mark - mark) / mark * 10000 >= Decimal("0.5"):
                    return  # Recompute both quotes using fresh market data.
            cl_id = self.send(side, qty, target)
            if cl_id:
                self.active_entry_ids.add(cl_id)
            # A newly placed entry could have filled before the second side.
            if not DRY_RUN and self.get_position()[0] != 0:
                return

    def close_filled_position(self):
        # A resting opposite-side entry may fill too. Cancel and verify all
        # bot entries before determining the final quantity to reduce.
        self.cancel_owned(self.all_open_orders())
        current_qty, _ = self.get_position()
        if current_qty == 0:
            return
        if DRY_RUN:
            self.log(f"[SIMULATION] reduce-only market close {current_qty}")
            return
        if self.exit_submitted:
            raise RuntimeError("Prior market close is unconfirmed; will not send a duplicate")
        payload = {
            "symbol": SYMBOL,
            "side": "sell" if current_qty > 0 else "buy",
            "order_type": "market",
            "time_in_force": "ioc",
            "qty": format(abs(current_qty), "f"),
            "reduce_only": True,
        }
        self.log(f"Filled position {current_qty}; submitting reduce-only market close")
        # The response is asynchronous. Never send another close blindly if
        # the request times out or if only part of the position is reduced.
        self.exit_submitted = True
        unwrap(self.client._post_signed("/api/new_order", payload))
        for _ in range(20):
            time.sleep(0.25)
            if self.get_position()[0] == 0:
                self.exit_submitted = False
                self.entry_submitted = False
                self.active_entry_ids.clear()
                self.log("Position closed and verified; maker quoting may resume")
                return
        raise RuntimeError("Market close unconfirmed or partial. Bot stopped; inspect StandX position immediately")

    def run(self):
        try:
            startup_orders = self.all_open_orders()
            if startup_orders:
                raise RuntimeError(
                    f"Existing {SYMBOL} orders detected. Review them in StandX before starting."
                )
            if self.get_position()[0]:
                raise RuntimeError("Existing position detected before start. Manage it in StandX; no new entries sent")
            self.log(f"Mode={'SIMULATION' if DRY_RUN else 'LIVE'}, {SYMBOL}, "
                     f"entry={TARGET_BPS} bps, leverage={self.leverage}x")
            while not self.stop_event.is_set():
                mark = self.get_mark()
                qty, entry = self.get_position()
                orders = self.all_open_orders()
                balance = self.get_balance()
                self.status({
                    "mark": str(mark), "qty": str(qty), "entry": str(entry),
                    "equity": str(balance["equity"]),
                    "available": str(balance["cross_available"]),
                    "orders": orders, "leverage": self.leverage,
                })
                unknown = [x for x in orders if x not in self.owned(orders)]
                if unknown:
                    raise RuntimeError("Unknown order appeared; stopped")
                if qty:
                    if not self.entry_submitted:
                        raise RuntimeError("Unrecognized position detected; no automatic close")
                    self.close_filled_position()
                else:
                    exits = [x for x in self.owned(orders) if x.get("reduce_only")]
                    if exits:
                        self.cancel_owned(exits)
                        orders = self.all_open_orders() if not DRY_RUN else []
                    if not self.stop_event.is_set():
                        self.manage_entry(mark, orders)
                self.stop_event.wait(LOOP_SECONDS)
        except Exception as exc:
            self.log(f"STOPPED: {exc}")
        finally:
            # A fill may race with Stop, order placement, or cancellation.
            try:
                if DRY_RUN:
                    self.log("Simulation stopped; no live orders were sent.")
                else:
                    for _ in range(3):
                        orders = self.all_open_orders()
                        owned = self.owned(orders)
                        if not owned:
                            break
                        for order in owned:
                            self.cancel(order)
                    remaining = self.owned(self.all_open_orders())
                    if remaining:
                        raise RuntimeError(f"{len(remaining)} bot orders still open")
                    self.log("All bot-owned open orders canceled and verified.")
                position_qty, _ = self.get_position()
                if position_qty and self.entry_submitted and not self.exit_submitted:
                    self.log("Position detected during shutdown; attempting immediate close")
                    self.close_filled_position()
                elif position_qty:
                    self.log(f"POSITION STILL OPEN ({position_qty} {SYMBOL}); manage it in StandX immediately")
            except Exception as exc:
                self.log(f"CANCEL NOT CONFIRMED: {exc}. Check orders and positions in StandX immediately.")


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
            "live": "Enable live orders",
            "start": "Start bot", "stop": "Stop and cancel orders",
            "orders": "Open orders", "logs": "Activity log", "side": "Side",
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
            "leverage_error": "Leverage exceeds this market's current limit.",
            "live_title": "Live trading",
            "live_confirm": "{symbol} · {target} BPS · balance {margin}% · leverage {leverage}x\n\nFilled entries will be closed with reduce-only market orders. Fees and slippage apply. Send real orders?",
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
            "live": "Canlı emirleri etkinleştir",
            "start": "Botu başlat", "stop": "Durdur ve emirleri iptal et",
            "orders": "Açık emirler", "logs": "İşlem günlüğü", "side": "Yön",
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
            "leverage_error": "Kaldıraç pazarın güncel sınırını aşamaz.",
            "live_title": "Canlı işlem",
            "live_confirm": "{symbol} · {target} BPS · bakiye %{margin} · kaldıraç {leverage}x\n\nDolmuş girişler azaltıcı market emirle kapatılacak. Ücret ve kayma olabilir. Gerçek emir gönderilsin mi?",
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
        self.stop_event = threading.Event()
        self.closing = False
        self.language = tk.StringVar(value="EN")
        self.market = tk.StringVar(value="BTC-USD")
        self.target = tk.StringVar(value="5.5")
        self.lower = tk.StringVar(value="5")
        self.upper = tk.StringVar(value="6")
        self.margin = tk.StringVar(value="10")
        self.leverage_limit = tk.StringVar(value="—")
        self.market_limit = None
        self.live = tk.BooleanVar(value=False)
        self.state = tk.StringVar()
        self.market_data = tk.StringVar(value="Mark: —    Leverage: —    Position: —")
        self.account_data = tk.StringVar(value="Equity: —    Available: —")
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
                  ("margin", self.margin), ("leverage", self.leverage_limit)]
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
        max_leverage = int(self.leverage_limit.get().strip())
        if not (Decimal(0) < lower <= target <= upper < Decimal(10)):
            raise ValueError(self.t("bps_error"))
        if not (Decimal(0) < margin <= Decimal(100)):
            raise ValueError(self.t("margin_error"))
        if self.market_limit is None or not 1 <= max_leverage <= self.market_limit:
            raise ValueError(self.t("leverage_error"))
        return target, lower, upper, margin / 100, max_leverage

    def start(self):
        global SYMBOL, TARGET_BPS, MIN_BPS, MAX_BPS, MAX_MARGIN_FRACTION
        global MAX_LEVERAGE, DRY_RUN, PREFIX
        if self.worker and self.worker.is_alive():
            return
        if self.market_limit is None:
            messagebox.showerror(self.t("market_title"), self.t("market_wait"))
            return
        try:
            (TARGET_BPS, MIN_BPS, MAX_BPS, MAX_MARGIN_FRACTION,
             MAX_LEVERAGE) = self.settings()
        except (ValueError, ArithmeticError) as exc:
            messagebox.showerror(self.t("invalid"), str(exc))
            return
        SYMBOL = self.market.get()
        DRY_RUN = not self.live.get()
        PREFIX = "SM-" + SYMBOL.replace("-", "") + "-"
        if not DRY_RUN:
            details = self.t("live_confirm", symbol=SYMBOL, target=TARGET_BPS,
                             margin=MAX_MARGIN_FRACTION * 100, leverage=MAX_LEVERAGE)
            if not messagebox.askyesno(self.t("live_title"), details):
                return
        self.stop_event = threading.Event()
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
            self.events.put(("state", "running" if not DRY_RUN else "simulation"))
            bot.run()
        except Exception as exc:
            self.events.put(("log", f"{self.t('startup_error')}: {exc}"))
        finally:
            self.events.put(("finished", None))

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.state.set(self.t("stopping"))
            self.stop_button.configure(state="disabled")
            self.stop_event.set()

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
                elif kind == "state":
                    self.state.set(self.t(payload))
                elif kind == "status":
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
                    self.state.set(self.t("stopped"))
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
