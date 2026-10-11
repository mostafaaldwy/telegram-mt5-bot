# -*- coding: utf-8 -*-
"""
NEXUS MT5 BOT
-------------
Telegram (Telethon)
        ↓
Signal Parser
        ↓
Independent Trade States
        ↓
Risk Manager
        ↓
MT5 Bridge
        ↓
Dry-Run / Real Execution

REAL TRADING IS DISABLED BY DEFAULT.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Dict, List, Tuple, Any

# ============================================================
# IMPORTS
# ============================================================

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

try:
    from telethon import TelegramClient, events
except ImportError:
    TelegramClient = None
    events = None


# ============================================================
# CONFIG
# ============================================================

# ---------------- Telegram ----------------

TELEGRAM_API_ID = int(
    os.getenv("TELEGRAM_API_ID", "0")
)

TELEGRAM_API_HASH = os.getenv(
    "TELEGRAM_API_HASH", ""
)

TELEGRAM_SESSION = os.getenv(
    "TELEGRAM_SESSION",
    "telegram_session"
)

TELEGRAM_CHANNEL = os.getenv(
    "TELEGRAM_CHANNEL",
    ""
)


# ---------------- MT5 ----------------

MT5_PATH = os.getenv(
    "MT5_PATH",
    r"C:\Program Files\MetaTrader 5\terminal64.exe"
)


# ---------------- Risk ----------------

ACCOUNT_BALANCE = float(
    os.getenv("ACCOUNT_BALANCE", "10000")
)

RISK_PERCENT = float(
    os.getenv("RISK_PERCENT", "0.25")
)

DAILY_MAX_LOSS_PERCENT = float(
    os.getenv("DAILY_MAX_LOSS_PERCENT", "3")
)


# ---------------- Safety ----------------

# IMPORTANT:
# Keep False until the entire system is validated.
LIVE_TRADING = False

AUTO_MOVE_SL = True

AUTO_PARTIAL_CLOSE = False

AUTO_FULL_CLOSE = True

PARTIAL_CLOSE_PERCENT = 0.50


# ---------------- System ----------------

STARTUP_HISTORY_LIMIT = int(
    os.getenv("STARTUP_HISTORY_LIMIT", "0")
)

SUPPORTED_SYMBOLS = {
    "XAUUSD",
    "USDCHF",
    "NZDJPY",
    "GBPCAD",
}

SYMBOL_ALIASES = {
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
}


# ============================================================
# HELPERS
# ============================================================

def now_str() -> str:
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    return SYMBOL_ALIASES.get(
        symbol,
        symbol
    )


def safe_float(text: str) -> Optional[float]:
    try:
        return float(
            text.replace(",", "")
        )
    except Exception:
        return None


def floor_to_step(
    value: float,
    step: float
) -> float:

    if step <= 0:
        return value

    return math.floor(
        (value / step) + 1e-12
    ) * step


def volume_decimals(step: float) -> int:

    text = (
        f"{step:.10f}"
        .rstrip("0")
    )

    if "." not in text:
        return 0

    return len(
        text.split(".")[1]
    )


def normalize_volume_down(
    volume: float,
    min_volume: float,
    max_volume: float,
    step: float,
) -> float:

    if step <= 0:
        return max(
            min_volume,
            min(max_volume, volume)
        )

    volume = floor_to_step(
        volume,
        step
    )

    volume = min(
        max_volume,
        volume
    )

    if volume < min_volume:
        return 0.0

    return round(
        volume,
        volume_decimals(step)
    )


# ============================================================
# TRADE STATE
# ============================================================

@dataclass
class TradeState:

    trade_id: str

    source_message_id: int

    created_at: str

    symbol: str

    side: str

    entry: float

    sl: float

    tp1: float

    tp2: float

    valid: bool = True

    open: bool = True

    status: str = "OPEN_READY"

    sl_moved_to_entry: bool = False

    tp1_hit: bool = False

    tp2_hit: bool = False

    partial_close_recommended: bool = False

    partial_closed: bool = False

    breakeven_hit: bool = False

    close_reason: Optional[str] = None

    requested_volume: float = 0.0

    executed_volume: float = 0.0

    mt5_ticket: Optional[int] = None

    events: List[Dict[str, Any]] = field(
        default_factory=list
    )

    def add_event(
        self,
        event_type: str,
        message_id: Optional[int],
        details: str = ""
    ):

        self.events.append({

            "time": now_str(),

            "event": event_type,

            "message_id": message_id,

            "details": details,

        })

    def mark_closed(
        self,
        reason: str
    ):

        self.open = False

        self.close_reason = reason

        if reason == "CLOSE_AT_ENTRY":

            self.status = (
                "CLOSED_AT_ENTRY"
            )

        elif reason == "BREAKEVEN_HIT":

            self.status = (
                "CLOSED_BREAKEVEN"
            )

        elif reason == "SIGNAL_CLOSE":

            self.status = "CLOSED"

        elif reason == "TP2":

            self.status = "CLOSED_TP2"

        elif reason == "SL":

            self.status = "CLOSED_SL"

        else:

            self.status = "CLOSED"

    def status_summary(self) -> str:

        return (

            f"{self.trade_id} | "

            f"{self.symbol} {self.side} | "

            f"Entry={self.entry} "

            f"SL={self.sl} "

            f"TP1={self.tp1} "

            f"TP2={self.tp2} | "

            f"{self.status} | "

            f"open={self.open} | "

            f"BE={self.sl_moved_to_entry} "

            f"TP1={self.tp1_hit} "

            f"TP2={self.tp2_hit} | "

            f"partial_rec="
            f"{self.partial_close_recommended} "

            f"partial_closed="
            f"{self.partial_closed} | "

            f"BE_hit="
            f"{self.breakeven_hit}"

        )


# ============================================================
# SIGNAL PARSER
# ============================================================

class SignalParser:

    SIGNAL_RE = re.compile(
        r"\b(BUY|SELL)\s+([A-Z]{3,8})\s+NOW\b",
        re.IGNORECASE
    )

    AT_RE = re.compile(
        r"\bAT\s*[:=]?\s*(-?\d+(?:\.\d+)?)",
        re.IGNORECASE
    )

    TP1_RE = re.compile(
        r"\bTP1\s*[:=]?\s*(-?\d+(?:\.\d+)?)",
        re.IGNORECASE
    )

    TP2_RE = re.compile(
        r"\bTP2\s*[:=]?\s*(-?\d+(?:\.\d+)?)",
        re.IGNORECASE
    )

    SL_RE = re.compile(
        r"\bSL\s*[:=]?\s*(-?\d+(?:\.\d+)?)",
        re.IGNORECASE
    )

    def __init__(self):

        self.pending_signal = None

    def extract_fields(
        self,
        text: str
    ):

        return {

            "entry":
                next(
                    (
                        safe_float(m.group(1))
                        for m in self.AT_RE.finditer(text)
                    ),
                    None
                ),

            "tp1":
                next(
                    (
                        safe_float(m.group(1))
                        for m in self.TP1_RE.finditer(text)
                    ),
                    None
                ),

            "tp2":
                next(
                    (
                        safe_float(m.group(1))
                        for m in self.TP2_RE.finditer(text)
                    ),
                    None
                ),

            "sl":
                next(
                    (
                        safe_float(m.group(1))
                        for m in self.SL_RE.finditer(text)
                    ),
                    None
                ),
        }

    def detect_signal(
        self,
        text: str
    ) -> Optional[Tuple[str, str]]:

        match = self.SIGNAL_RE.search(
            text.upper()
        )

        if not match:
            return None

        return (
            match.group(1).upper(),
            normalize_symbol(
                match.group(2)
            )
        )

    def parse_message(
        self,
        message_id: int,
        text: str
    ) -> List[Dict[str, Any]]:

        output = []

        clean = (
            text or ""
        ).strip()

        if not clean:
            return output

        signal = self.detect_signal(
            clean
        )

        # ==============================================
        # NEW SIGNAL
        # ==============================================

        if signal:

            side, symbol = signal

            fields = self.extract_fields(
                clean
            )

            self.pending_signal = {

                "source_message_id":
                    message_id,

                "created_at":
                    now_str(),

                "side":
                    side,

                "symbol":
                    symbol,

                "entry":
                    fields["entry"],

                "tp1":
                    fields["tp1"],

                "tp2":
                    fields["tp2"],

                "sl":
                    fields["sl"],
            }

            # Complete signal in one message

            if all(
                self.pending_signal[x]
                is not None

                for x in (
                    "entry",
                    "tp1",
                    "tp2",
                    "sl"
                )
            ):

                output.append({

                    "type":
                        "NEW_SIGNAL",

                    **self.pending_signal

                })

                self.pending_signal = None

            return output


        # ==============================================
        # SIGNAL DETAILS
        # ==============================================

        if self.pending_signal:

            fields = self.extract_fields(
                clean
            )

            changed = False

            for key, value in fields.items():

                if value is not None:

                    self.pending_signal[key] = value

                    changed = True

            if (
                changed
                and
                all(
                    self.pending_signal[x]
                    is not None

                    for x in (
                        "entry",
                        "tp1",
                        "tp2",
                        "sl"
                    )
                )
            ):

                output.append({

                    "type":
                        "NEW_SIGNAL",

                    **self.pending_signal

                })

                self.pending_signal = None

                return output


        # ==============================================
        # MANAGEMENT
        # ==============================================

        event_type = self.detect_management(
            clean
        )

        if event_type:

            output.append({

                "type":
                    event_type,

                "message_id":
                    message_id,

                "text":
                    clean,

                "profit_points":
                    self.parse_profit_update(
                        clean
                    ),

            })

        return output


    def detect_management(
        self,
        text: str
    ) -> Optional[str]:

        t = text.strip().lower()


        # CLOSE AT ENTRY
        if (
            "اغلاق" in t
            and
            (
                "دخول" in t
                or
                "على دخول" in t
                or
                "علي دخول" in t
            )
        ):

            return "CLOSE_AT_ENTRY"


        # MOVE SL TO ENTRY
        if (
            "تامين" in t
            and
            "دخول" in t
        ):

            return "MOVE_SL_TO_ENTRY"


        # BREAKEVEN HIT
        if (
            "ضربت تامين" in t
            or
            "ضرب التأمين" in t
        ):

            return "BREAKEVEN_HIT"


        # TP1
        if (
            "ضرب الهدف الاول" in t
            or
            "ضرب الهدف الأول" in t
        ):

            return "TP1_HIT"


        # TP2
        if (
            "ضرب الهدف الثاني" in t
            or
            "ضرب الهدف 2" in t
            or
            "tp2 hit" in t
        ):

            return "TP2_HIT"


        # PARTIAL
        if (
            "حجز نصف الارباح" in t
            or
            "حجز نصف الأرباح" in t
        ):

            return "PARTIAL_CLOSE_RECOMMENDED"


        # FULL CLOSE
        if (
            "اغلاق صفقه الان" in t
            or
            "إغلاق صفقة الآن" in t
            or
            "close trade" in t
        ):

            return "CLOSE"


        # PROFIT UPDATE
        if (
            "نقطه" in t
            or
            "نقطة" in t
            or
            "points" in t
            or
            "pips" in t
        ):

            return "PROFIT_UPDATE"


        return None


    @staticmethod
    def parse_profit_update(
        text: str
    ) -> Optional[float]:

        patterns = [

            r"(\d+(?:\.\d+)?)\s*\+?\s*نقطة",

            r"(\d+(?:\.\d+)?)\s*\+?\s*نقطه",

            r"\+?\s*(\d+(?:\.\d+)?)\s*points?",

            r"\+?\s*(\d+(?:\.\d+)?)\s*pips?",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
                re.IGNORECASE
            )

            if match:

                return safe_float(
                    match.group(1)
                )

        return None


# ============================================================
# TRADE STATE MANAGER
# ============================================================

class TradeStateManager:

    def __init__(self):

        self.trade_states = {}


    @staticmethod
    def make_trade_id(
        message_id: int
    ) -> str:

        return f"TRADE-{message_id}"


    def create_trade(
        self,
        parsed: Dict[str, Any]
    ) -> TradeState:

        trade_id = (
            self.make_trade_id(
                parsed["source_message_id"]
            )
        )

        entry = float(
            parsed["entry"]
        )

        sl = float(
            parsed["sl"]
        )

        tp1 = float(
            parsed["tp1"]
        )

        tp2 = float(
            parsed["tp2"]
        )

        side = parsed["side"]

        valid = True


        # -----------------------------
        # Validate BUY
        # -----------------------------

        if side == "BUY":

            if not (
                sl
                <
                entry
                <
                tp1
                <=
                tp2
            ):

                valid = False


        # -----------------------------
        # Validate SELL
        # -----------------------------

        elif side == "SELL":

            if not (
                sl
                >
                entry
                >
                tp1
                >=
                tp2
            ):

                valid = False


        else:

            valid = False


        trade = TradeState(

            trade_id=trade_id,

            source_message_id=
                int(
                    parsed[
                        "source_message_id"
                    ]
                ),

            created_at=
                str(
                    parsed[
                        "created_at"
                    ]
                ),

            symbol=
                normalize_symbol(
                    parsed["symbol"]
                ),

            side=side,

            entry=entry,

            sl=sl,

            tp1=tp1,

            tp2=tp2,

            valid=valid,

            open=valid,

            status=
                (
                    "OPEN_READY"
                    if valid
                    else
                    "INVALID"
                )
        )


        trade.add_event(

            "NEW_SIGNAL",

            trade.source_message_id,

            (
                "Parsed and validated"
                if valid
                else
                "Invalid price structure"
            )

        )

        self.trade_states[
            trade.trade_id
        ] = trade

        return trade


    def latest_open_trade(
        self,
        symbol: Optional[str] = None
    ) -> Optional[TradeState]:

        candidates = [

            t

            for t
            in self.trade_states.values()

            if
            t.open
            and
            t.valid
            and
            (
                symbol is None
                or
                t.symbol == symbol
            )

        ]

        if not candidates:

            return None

        return max(
            candidates,
            key=lambda x:
                x.source_message_id
        )


    def apply_event(
        self,
        event: Dict[str, Any]
    ) -> Optional[TradeState]:

        event_type = event["type"]


        # ==============================================
        # NEW TRADE
        # ==============================================

        if event_type == "NEW_SIGNAL":

            return self.create_trade(
                event
            )


        symbol = None

        text = event.get(
            "text",
            ""
        )


        # Detect symbol inside management msg

        match = re.search(

            r"\b("
            r"XAUUSD|"
            r"USDCHF|"
            r"NZDJPY|"
            r"GBPCAD|"
            r"GOLD"
            r")\b",

            text.upper()
        )


        if match:

            symbol = normalize_symbol(
                match.group(1)
            )


        trade = self.latest_open_trade(
            symbol
        )


        if not trade:

            return None


        message_id = event.get(
            "message_id"
        )


        trade.add_event(

            event_type,

            message_id,

            text

        )


        # ==============================================
        # MOVE SL TO ENTRY
        # ==============================================

        if event_type == "MOVE_SL_TO_ENTRY":

            trade.sl_moved_to_entry = True

            trade.sl = trade.entry

            if trade.open:

                trade.status = (
                    "OPEN_PROTECTED"
                )


        # ==============================================
        # CLOSE AT ENTRY
        # ==============================================

        elif event_type == "CLOSE_AT_ENTRY":

            trade.sl_moved_to_entry = True

            trade.sl = trade.entry

            trade.mark_closed(
                "CLOSE_AT_ENTRY"
            )


        # ==============================================
        # BREAKEVEN HIT
        # ==============================================

        elif event_type == "BREAKEVEN_HIT":

            trade.sl_moved_to_entry = True

            trade.breakeven_hit = True

            trade.mark_closed(
                "BREAKEVEN_HIT"
            )


        # ==============================================
        # TP1
        # ==============================================

        elif event_type == "TP1_HIT":

            trade.tp1_hit = True

            if trade.open:

                trade.status = (
                    "OPEN_AFTER_TP1"
                )


        # ==============================================
        # TP2
        # ==============================================

        elif event_type == "TP2_HIT":

            trade.tp2_hit = True

            trade.mark_closed(
                "TP2"
            )


        # ==============================================
        # PARTIAL
        # ==============================================

        elif (
            event_type
            ==
            "PARTIAL_CLOSE_RECOMMENDED"
        ):

            trade.partial_close_recommended = True

            if (
                AUTO_PARTIAL_CLOSE
                and
                trade.open
                and
                not trade.partial_closed
            ):

                trade.partial_closed = True


        # ==============================================
        # CLOSE
        # ==============================================

        elif event_type == "CLOSE":

            trade.mark_closed(
                "SIGNAL_CLOSE"
            )


        # ==============================================
        # PROFIT UPDATE
        # ==============================================

        elif event_type == "PROFIT_UPDATE":

            if trade.open:

                trade.status = (
                    "PROFIT_UPDATE"
                )


        return trade


    def all_states(self):

        return sorted(

            self.trade_states.values(),

            key=lambda x:
                x.source_message_id

        )


    def connected_trade_states(self):

        return [

            t

            for t
            in self.all_states()

            if t.valid

        ]


    def print_summary(self):

        print(
            "\n"
            + "=" * 100
        )

        print(
            "TRADE STATES SUMMARY"
        )

        print(
            "=" * 100
        )


        states = self.all_states()


        if not states:

            print(
                "No trade states."
            )

            return


        for trade in states:

            print(
                trade.status_summary()
            )


        print(
            "=" * 100
        )


# ============================================================
# RISK MANAGER
# ============================================================

class RiskManager:

    def __init__(

        self,

        account_balance=
            ACCOUNT_BALANCE,

        risk_percent=
            RISK_PERCENT,

        daily_max_loss_percent=
            DAILY_MAX_LOSS_PERCENT

    ):

        self.account_balance = (
            float(account_balance)
        )

        self.risk_percent = (
            float(risk_percent)
        )

        self.daily_max_loss_percent = (
            float(daily_max_loss_percent)
        )

        self.daily_loss_amount = 0.0

        self.loss_day = date.today()


    @property
    def risk_amount_per_trade(self):

        return (
            self.account_balance
            *
            (
                self.risk_percent
                /
                100
            )
        )


    @property
    def daily_max_loss_amount(self):

        return (
            self.account_balance
            *
            (
                self.daily_max_loss_percent
                /
                100
            )
        )


    def reset_if_new_day(self):

        today = date.today()

        if today != self.loss_day:

            self.loss_day = today

            self.daily_loss_amount = 0.0


    def add_realized_loss(
        self,
        loss_amount: float
    ):

        self.reset_if_new_day()

        self.daily_loss_amount += max(
            0.0,
            float(loss_amount)
        )


    def daily_limit_reached(self):

        self.reset_if_new_day()

        return (
            self.daily_loss_amount
            >=
            self.daily_max_loss_amount
        )


    def can_open_new_trade(
        self
    ) -> Tuple[bool, str]:

        self.reset_if_new_day()


        if self.daily_limit_reached():

            return (

                False,

                (
                    "DAILY LOSS LIMIT REACHED: "
                    f"${self.daily_loss_amount:.2f} / "
                    f"${self.daily_max_loss_amount:.2f}"
                )

            )


        return True, "OK"


    def summary(self):

        self.reset_if_new_day()

        return (

            f"Risk/trade="
            f"${self.risk_amount_per_trade:.2f} | "

            f"Daily max loss="
            f"${self.daily_max_loss_amount:.2f} | "

            f"Today's loss="
            f"${self.daily_loss_amount:.2f}"

        )


# ============================================================
# MT5 BRIDGE
# ============================================================

class MT5Bridge:

    def __init__(
        self,
        path: str = MT5_PATH
    ):

        self.path = path

        self.connected = False


    def connect(self):

        if mt5 is None:

            raise RuntimeError(

                "MetaTrader5 is not installed.\n"

                "Run:\n"

                "python -m pip install MetaTrader5"

            )


        kwargs = {}


        if (
            self.path
            and
            os.path.exists(
                self.path
            )
        ):

            kwargs["path"] = self.path


        if not mt5.initialize(
            **kwargs
        ):

            raise RuntimeError(

                f"MT5 initialize failed: "
                f"{mt5.last_error()}"

            )


        self.connected = True

        return True


    def shutdown(self):

        if mt5 is not None:

            mt5.shutdown()

        self.connected = False


    def ensure_symbol(
        self,
        symbol: str
    ):

        symbol = normalize_symbol(
            symbol
        )


        info = mt5.symbol_info(
            symbol
        )


        if info is None:

            raise RuntimeError(

                f"Symbol not found in MT5: "
                f"{symbol}"

            )


        if not info.visible:

            selected = (
                mt5.symbol_select(
                    symbol,
                    True
                )
            )


            if not selected:

                raise RuntimeError(

                    f"Could not activate "
                    f"{symbol} in Market Watch"

                )


            info = mt5.symbol_info(
                symbol
            )


        return info


    def symbol_specs(
        self,
        symbol: str
    ):

        info = self.ensure_symbol(
            symbol
        )

        tick = mt5.symbol_info_tick(
            symbol
        )


        if tick is None:

            raise RuntimeError(

                f"No tick data for "
                f"{symbol}"

            )


        return {

            "symbol":
                symbol,

            "visible":
                bool(info.visible),

            "digits":
                int(info.digits),

            "point":
                float(info.point),

            "tick_size":
                float(
                    info.trade_tick_size
                    or
                    info.point
                ),

            "tick_value":
                float(
                    info.trade_tick_value
                ),

            "volume_min":
                float(
                    info.volume_min
                ),

            "volume_max":
                float(
                    info.volume_max
                ),

            "volume_step":
                float(
                    info.volume_step
                ),

            "contract_size":
                float(
                    info.trade_contract_size
                ),

            "bid":
                float(tick.bid),

            "ask":
                float(tick.ask),

        }


    def account_info(self):

        acc = mt5.account_info()


        if acc is None:

            raise RuntimeError(

                f"MT5 account_info failed: "
                f"{mt5.last_error()}"

            )


        return {

            "login":
                int(acc.login),

            "server":
                str(acc.server),

            "balance":
                float(acc.balance),

            "equity":
                float(acc.equity),

            "margin":
                float(acc.margin),

            "free_margin":
                float(
                    acc.margin_free
                ),

            "currency":
                str(acc.currency),

        }


    def quote(
        self,
        symbol: str
    ):

        self.ensure_symbol(
            symbol
        )

        tick = mt5.symbol_info_tick(
            symbol
        )


        if tick is None:

            raise RuntimeError(
                f"No quote for {symbol}"
            )


        return {

            "bid":
                float(tick.bid),

            "ask":
                float(tick.ask),

            "last":
                float(
                    getattr(
                        tick,
                        "last",
                        0.0
                    )
                ),

        }


    # ========================================================
    # REAL RISK CALCULATION
    # ========================================================

    def calc_loss_per_lot(
        self,
        symbol: str,
        side: str,
        entry: float,
        sl: float
    ):

        self.ensure_symbol(
            symbol
        )


        order_type = (

            mt5.ORDER_TYPE_BUY

            if side.upper() == "BUY"

            else

            mt5.ORDER_TYPE_SELL

        )


        result = mt5.order_calc_profit(

            order_type,

            symbol,

            1.0,

            float(entry),

            float(sl)

        )


        if result is not None:

            loss = abs(
                float(result)
            )

            if loss > 0:

                return loss


        # Fallback

        info = mt5.symbol_info(
            symbol
        )


        if info is None:

            raise RuntimeError(
                f"No symbol info for {symbol}"
            )


        tick_size = float(

            info.trade_tick_size

            or

            info.point

        )


        tick_value = float(

            info.trade_tick_value

        )


        if (
            tick_size <= 0
            or
            tick_value <= 0
        ):

            raise RuntimeError(

                f"Invalid tick specs "
                f"for {symbol}"

            )


        distance = abs(

            float(entry)
            -
            float(sl)

        )


        return (

            distance
            /
            tick_size
            *
            tick_value

        )


    def calculate_lot_size(

        self,

        symbol: str,

        side: str,

        entry: float,

        sl: float,

        risk_amount: Optional[float] = None

    ):

        info = self.ensure_symbol(
            symbol
        )


        if risk_amount is None:

            risk_amount = (

                ACCOUNT_BALANCE
                *
                RISK_PERCENT
                /
                100

            )


        risk_amount = float(
            risk_amount
        )


        loss_per_lot = (
            self.calc_loss_per_lot(

                symbol,
                side,
                entry,
                sl

            )
        )


        if loss_per_lot <= 0:

            raise RuntimeError(

                f"Invalid loss per lot "
                f"for {symbol}"

            )


        raw_volume = (

            risk_amount
            /
            loss_per_lot

        )


        volume = normalize_volume_down(

            raw_volume,

            float(
                info.volume_min
            ),

            float(
                info.volume_max
            ),

            float(
                info.volume_step
            )

        )


        actual_risk = (

            volume
            *
            loss_per_lot

        )


        return {

            "symbol":
                symbol,

            "side":
                side,

            "entry":
                float(entry),

            "sl":
                float(sl),

            "risk_amount":
                risk_amount,

            "loss_per_1_lot":
                loss_per_lot,

            "raw_volume":
                raw_volume,

            "volume":
                volume,

            "actual_planned_risk":
                actual_risk,

            "volume_min":
                float(
                    info.volume_min
                ),

            "volume_max":
                float(
                    info.volume_max
                ),

            "volume_step":
                float(
                    info.volume_step
                ),

        }


    # ========================================================
    # REAL ORDER FUNCTIONS
    # ========================================================

    def _order_type(
        self,
        side: str
    ):

        return (

            mt5.ORDER_TYPE_BUY

            if side.upper() == "BUY"

            else

            mt5.ORDER_TYPE_SELL

        )


    def market_price_for_side(

        self,
        symbol: str,
        side: str

    ):

        q = self.quote(
            symbol
        )

        return (

            q["ask"]

            if side.upper() == "BUY"

            else

            q["bid"]

        )


    def open_market_order(

        self,
        trade: TradeState,
        volume: float,
        deviation: int = 20,
        magic: int = 26082026

    ):

        symbol = trade.symbol

        self.ensure_symbol(
            symbol
        )


        order_type = (
            self._order_type(
                trade.side
            )
        )


        price = (
            self.market_price_for_side(
                symbol,
                trade.side
            )
        )


        request = {

            "action":
                mt5.TRADE_ACTION_DEAL,

            "symbol":
                symbol,

            "volume":
                float(volume),

            "type":
                order_type,

            "price":
                price,

            "sl":
                float(trade.sl),

            "tp":
                float(trade.tp2),

            "deviation":
                deviation,

            "magic":
                magic,

            "comment":
                trade.trade_id,

            "type_time":
                mt5.ORDER_TIME_GTC,

            "type_filling":
                mt5.ORDER_FILLING_IOC,

        }


        return mt5.order_send(
            request
        )


    def modify_position_sl(

        self,
        ticket: int,
        symbol: str,
        new_sl: float,
        new_tp: Optional[float] = None

    ):

        positions = mt5.positions_get(
            ticket=ticket
        )


        if not positions:

            raise RuntimeError(
                f"Position not found: {ticket}"
            )


        position = positions[0]


        tp = (

            float(position.tp)

            if new_tp is None

            else

            float(new_tp)

        )


        request = {

            "action":
                mt5.TRADE_ACTION_SLTP,

            "symbol":
                symbol,

            "position":
                int(ticket),

            "sl":
                float(new_sl),

            "tp":
                tp,

        }


        return mt5.order_send(
            request
        )


    def close_position(

        self,
        ticket: int,
        deviation: int = 20,
        magic: int = 26082026

    ):

        positions = mt5.positions_get(
            ticket=ticket
        )


        if not positions:

            raise RuntimeError(
                f"Position not found: {ticket}"
            )


        p = positions[0]


        close_type = (

            mt5.ORDER_TYPE_SELL

            if
            p.type == mt5.POSITION_TYPE_BUY

            else

            mt5.ORDER_TYPE_BUY

        )


        tick = mt5.symbol_info_tick(
            p.symbol
        )


        if tick is None:

            raise RuntimeError(
                f"No tick for {p.symbol}"
            )


        price = (

            tick.bid

            if
            close_type ==
            mt5.ORDER_TYPE_SELL

            else

            tick.ask

        )


        request = {

            "action":
                mt5.TRADE_ACTION_DEAL,

            "symbol":
                p.symbol,

            "volume":
                float(p.volume),

            "type":
                close_type,

            "position":
                int(ticket),

            "price":
                price,

            "deviation":
                deviation,

            "magic":
                magic,

            "comment":
                f"CLOSE-{ticket}",

            "type_time":
                mt5.ORDER_TIME_GTC,

            "type_filling":
                mt5.ORDER_FILLING_IOC,

        }


        return mt5.order_send(
            request
        )


    def partial_close(

        self,
        ticket: int,
        percent: float =
            PARTIAL_CLOSE_PERCENT,
        deviation: int = 20,
        magic: int = 26082026

    ):

        positions = mt5.positions_get(
            ticket=ticket
        )


        if not positions:

            raise RuntimeError(
                f"Position not found: {ticket}"
            )


        p = positions[0]


        volume_to_close = (

            float(p.volume)
            *
            float(percent)

        )


        info = self.ensure_symbol(
            p.symbol
        )


        volume_to_close = (
            normalize_volume_down(

                volume_to_close,

                float(
                    info.volume_min
                ),

                float(
                    info.volume_max
                ),

                float(
                    info.volume_step
                )

            )
        )


        if (

            volume_to_close <= 0

            or

            volume_to_close >=
            float(p.volume)

        ):

            raise RuntimeError(

                "Invalid partial-close volume"

            )


        close_type = (

            mt5.ORDER_TYPE_SELL

            if
            p.type ==
            mt5.POSITION_TYPE_BUY

            else

            mt5.ORDER_TYPE_BUY

        )


        tick = mt5.symbol_info_tick(
            p.symbol
        )


        if tick is None:

            raise RuntimeError(
                f"No tick for {p.symbol}"
            )


        price = (

            tick.bid

            if
            close_type ==
            mt5.ORDER_TYPE_SELL

            else

            tick.ask

        )


        request = {

            "action":
                mt5.TRADE_ACTION_DEAL,

            "symbol":
                p.symbol,

            "volume":
                volume_to_close,

            "type":
                close_type,

            "position":
                int(p.ticket),

            "price":
                price,

            "deviation":
                deviation,

            "magic":
                magic,

            "comment":
                f"PARTIAL-{ticket}",

            "type_time":
                mt5.ORDER_TIME_GTC,

            "type_filling":
                mt5.ORDER_FILLING_IOC,

        }


        return mt5.order_send(
            request
        )


# ============================================================
# EXECUTION ENGINE
# ============================================================

class ExecutionEngine:

    def __init__(

        self,

        mt5_bridge:
            Optional[MT5Bridge],

        risk_manager:
            RiskManager,

        live_trading:
            bool = LIVE_TRADING

    ):

        self.mt5_bridge = (
            mt5_bridge
        )

        self.risk_manager = (
            risk_manager
        )

        self.live_trading = (
            live_trading
        )


    def process_trade_entry(
        self,
        trade: TradeState
    ):

        if (
            not trade.valid
            or
            not trade.open
        ):

            print(

                f"ENTRY SKIPPED — "
                f"{trade.trade_id}"

            )

            return


        allowed, reason = (

            self.risk_manager
            .can_open_new_trade()

        )


        if not allowed:

            trade.add_event(

                "RISK_REJECTED",

                None,

                reason

            )

            print(

                f"ENTRY REJECTED — "
                f"{trade.trade_id} | "
                f"{reason}"

            )

            return


        if (
            self.mt5_bridge is None
            or
            mt5 is None
        ):

            print(

                f"ENTRY SIMULATION — "
                f"{trade.trade_id}"

            )

            return


        try:

            sizing = (

                self.mt5_bridge
                .calculate_lot_size(

                    symbol=
                        trade.symbol,

                    side=
                        trade.side,

                    entry=
                        trade.entry,

                    sl=
                        trade.sl,

                    risk_amount=
                        self.risk_manager
                        .risk_amount_per_trade

                )

            )


            volume = sizing["volume"]


            if volume <= 0:

                print(

                    f"ENTRY SKIPPED — "
                    f"{trade.trade_id} | "
                    f"Volume below minimum"

                )

                return


            trade.requested_volume = (
                volume
            )


            print(

                f"ENTRY READY — "
                f"{trade.trade_id} | "
                f"{trade.side} "
                f"{trade.symbol} | "
                f"Entry={trade.entry} | "
                f"SL={trade.sl} | "
                f"TP2={trade.tp2} | "
                f"Lot={volume:.2f} | "
                f"Risk≈$"
                f"{sizing['actual_planned_risk']:.2f}"

            )


            # ======================================
            # DRY RUN
            # ======================================

            if not self.live_trading:

                trade.executed_volume = (
                    volume
                )

                trade.add_event(

                    "DRY_RUN_ENTRY",

                    trade.source_message_id,

                    f"volume={volume}"

                )

                return


            # ======================================
            # REAL ORDER
            # ======================================

            result = (

                self.mt5_bridge
                .open_market_order(

                    trade=trade,

                    volume=volume

                )

            )


            if result is None:

                print(

                    f"ORDER FAILED — "
                    f"{mt5.last_error()}"

                )

                return


            trade.add_event(

                "MT5_ORDER_RESULT",

                trade.source_message_id,

                (
                    f"retcode={result.retcode}, "
                    f"order="
                    f"{getattr(result, 'order', None)}, "
                    f"deal="
                    f"{getattr(result, 'deal', None)}"
                )

            )


            if result.retcode == (
                mt5.TRADE_RETCODE_DONE
            ):

                trade.executed_volume = (
                    volume
                )

                trade.mt5_ticket = int(

                    getattr(
                        result,
                        "order",
                        0
                    )

                    or

                    getattr(
                        result,
                        "deal",
                        0
                    )

                )


                print(

                    f"LIVE ORDER OPENED — "
                    f"{trade.trade_id} | "
                    f"ticket="
                    f"{trade.mt5_ticket}"

                )

            else:

                print(

                    f"ORDER REJECTED — "
                    f"retcode="
                    f"{result.retcode}"

                )


        except Exception as exc:

            print(

                f"ENTRY ERROR — "
                f"{trade.trade_id}: "
                f"{exc}"

            )
    def process_management(
        self,
        trade: TradeState,
        event_type: str
    ):
        print(
            f"MANAGEMENT — "
            f"{trade.trade_id} | "
            f"{event_type}"
        )

        if not trade.open:
            print(
                f"Trade already closed: "
                f"{trade.close_reason}"
            )
            return

        # ======================================
        # MOVE SL TO ENTRY
        # ======================================

        if event_type == "MOVE_SL_TO_ENTRY":

            if (
                AUTO_MOVE_SL
                and self.mt5_bridge
                and trade.mt5_ticket
                and self.live_trading
            ):
                try:
                    result = self.mt5_bridge.modify_position_sl(
                        ticket=trade.mt5_ticket,
                        symbol=trade.symbol,
                        new_sl=trade.entry,
                        new_tp=trade.tp2,
                    )

                    print(
                        "SL modification retcode="
                        f"{getattr(result, 'retcode', None)}"
                    )

                except Exception as exc:
                    print(
                        f"SL modification error: {exc}"
                    )

        # ======================================
        # PARTIAL CLOSE
        # ======================================

        elif event_type == "PARTIAL_CLOSE_RECOMMENDED":

            if (
                AUTO_PARTIAL_CLOSE
                and self.mt5_bridge
                and trade.mt5_ticket
                and self.live_trading
                and not trade.partial_closed
            ):
                try:
                    result = self.mt5_bridge.partial_close(
                        ticket=trade.mt5_ticket,
                    )

                    print(
                        "Partial close retcode="
                        f"{getattr(result, 'retcode', None)}"
                    )

                except Exception as exc:
                    print(
                        f"Partial close error: {exc}"
                    )

        # ======================================
        # FULL CLOSE
        # ======================================

        elif event_type in {
            "CLOSE",
            "CLOSE_AT_ENTRY",
        }:

            if (
                AUTO_FULL_CLOSE
                and self.mt5_bridge
                and trade.mt5_ticket
                and self.live_trading
            ):
                try:
                    result = self.mt5_bridge.close_position(
                        ticket=trade.mt5_ticket,
                    )

                    print(
                        "Full close retcode="
                        f"{getattr(result, 'retcode', None)}"
                    )

                except Exception as exc:
                    print(
                        f"Close error: {exc}"
                    )    
# ============================================================
# MAIN BOT
# ============================================================

class NexusBot:

    def __init__(self):

        self.parser = (
            SignalParser()
        )

        self.state_manager = (
            TradeStateManager()
        )

        self.risk_manager = (
            RiskManager(

                account_balance=
                    ACCOUNT_BALANCE,

                risk_percent=
                    RISK_PERCENT,

                daily_max_loss_percent=
                    DAILY_MAX_LOSS_PERCENT

            )
        )

        self.mt5_bridge = (
            MT5Bridge(
                MT5_PATH
            )
        )

        self.execution = (
            ExecutionEngine(

                mt5_bridge=
                    self.mt5_bridge,

                risk_manager=
                    self.risk_manager,

                live_trading=
                    LIVE_TRADING

            )
        )

        self.telegram = None


    # ========================================================
    # MT5 START
    # ========================================================

    def start_mt5(self):

        print(
            "\n"
            + "=" * 80
        )

        print(
            "MT5 STARTUP"
        )

        print(
            "=" * 80
        )


        self.mt5_bridge.connect()


        account = (
            self.mt5_bridge
            .account_info()
        )


        print(
            "MT5 connected"
        )

        print(
            f"Login   : "
            f"{account['login']}"
        )

        print(
            f"Server  : "
            f"{account['server']}"
        )

        print(
            f"Balance : "
            f"{account['balance']}"
        )

        print(
            f"Equity  : "
            f"{account['equity']}"
        )

        print(
            f"Currency: "
            f"{account['currency']}"
        )


    # ========================================================
    # SYMBOL SPECS
    # ========================================================

    def print_symbol_specs(self):

        print(
            "\n"
            + "=" * 80
        )

        print(
            "MT5 SYMBOL SPECS"
        )

        print(
            "=" * 80
        )


        for symbol in sorted(
            SUPPORTED_SYMBOLS
        ):

            try:

                specs = (
                    self.mt5_bridge
                    .symbol_specs(
                        symbol
                    )
                )


                print(
                    f"\n{symbol}"
                )


                for key, value in (
                    specs.items()
                ):

                    print(
                        f"  "
                        f"{key:<18}"
                        f": {value}"
                    )


            except Exception as exc:

                print(
                    f"\n{symbol} "
                    f"-> unavailable: "
                    f"{exc}"
                )


    # ========================================================
    # PROCESS TELEGRAM MESSAGE
    # ========================================================

    def process_message(

        self,
        message_id: int,
        text: str

    ):

        events_out = (

            self.parser
            .parse_message(

                message_id=
                    message_id,

                text=
                    text

            )
        )


        for event in events_out:

            event_type = event["type"]


            # ==============================================
            # NEW SIGNAL
            # ==============================================

            if (
                event_type
                ==
                "NEW_SIGNAL"
            ):

                trade = (

                    self.state_manager
                    .apply_event(
                        event
                    )

                )


                if trade:

                    print(

                        "\nNEW TRADE:"
                    )

                    print(
                        trade.status_summary()
                    )


                    self.execution \
                        .process_trade_entry(
                            trade
                        )


            # ==============================================
            # MANAGEMENT
            # ==============================================

            else:

                trade = (

                    self.state_manager
                    .apply_event(
                        event
                    )

                )


                if trade:

                    self.execution \
                        .process_management(

                            trade,

                            event_type

                        )


                    print(
                        trade.status_summary()
                    )

                else:

                    print(

                        f"Management event "
                        f"without matching "
                        f"trade: "
                        f"{event_type}"

                    )


    # ========================================================
    # TELEGRAM HISTORY
    # ========================================================

    async def load_history(self):

        if (
            STARTUP_HISTORY_LIMIT
            <= 0
        ):

            return


        print(
            "\nLoading Telegram history..."
        )


        messages = []


        async for msg in (
            self.telegram
            .iter_messages(

                TELEGRAM_CHANNEL,

                limit=
                    STARTUP_HISTORY_LIMIT,

                reverse=True

            )
        ):

            if msg.message:

                messages.append(
                    msg
                )


        for msg in messages:

            self.process_message(

                message_id=
                    msg.id,

                text=
                    msg.message

            )


        self.state_manager \
            .print_summary()


    # ========================================================
    # TELEGRAM START
    # ========================================================

    async def start_telegram(self):

        if TelegramClient is None:

            raise RuntimeError(

                "Telethon not installed.\n"
                "Run:\n"
                "python -m pip install telethon"

            )


        if (
            not TELEGRAM_API_ID
            or
            not TELEGRAM_API_HASH
        ):

            raise RuntimeError(

                "TELEGRAM_API_ID "
                "and TELEGRAM_API_HASH "
                "are not configured."

            )


        if not TELEGRAM_CHANNEL:

            raise RuntimeError(

                "TELEGRAM_CHANNEL "
                "is not configured."

            )


        self.telegram = (
            TelegramClient(

                TELEGRAM_SESSION,

                TELEGRAM_API_ID,

                TELEGRAM_API_HASH

            )
        )


        await self.telegram.start()


        print(
            "\n"
            + "=" * 80
        )

        print(
            "TELEGRAM STARTUP"
        )

        print(
            "=" * 80
        )

        print(
            "Telegram connected"
        )

        print(
            f"Channel: "
            f"{TELEGRAM_CHANNEL}"
        )


        await self.load_history()


        @self.telegram.on(
            events.NewMessage(
                chats=
                    TELEGRAM_CHANNEL
            )
        )

        async def handler(event):

            msg = event.message

            text = (
                msg.message
                or
                ""
            )


            print(
                "\n"
                + "-" * 80
            )

            print(
                f"NEW TELEGRAM MESSAGE "
                f"#{msg.id}"
            )

            print(
                text
            )

            print(
                "-"
                * 80
            )


            self.process_message(

                message_id=
                    msg.id,

                text=
                    text

            )


        print(
            "\nWaiting for new Telegram messages..."
        )


        await (
            self.telegram
            .run_until_disconnected()
        )


    # ========================================================
    # RUN
    # ========================================================

    async def run(self):

        print(
            "\n"
            + "=" * 90
        )

        print(
            "NEXUS MT5 BOT"
        )

        print(
            "=" * 90
        )


        print(
            f"LIVE_TRADING    : "
            f"{LIVE_TRADING}"
        )

        print(
            f"AUTO_MOVE_SL    : "
            f"{AUTO_MOVE_SL}"
        )

        print(
            f"AUTO_PARTIAL    : "
            f"{AUTO_PARTIAL_CLOSE}"
        )

        print(
            f"AUTO_FULL_CLOSE : "
            f"{AUTO_FULL_CLOSE}"
        )

        print(
            f"RISK            : "
            f"{RISK_PERCENT}% "
            f"("
            f"${self.risk_manager.risk_amount_per_trade:.2f}"
            f"/trade)"
        )

        print(
            f"DAILY MAX LOSS  : "
            f"{DAILY_MAX_LOSS_PERCENT}% "
            f"("
            f"${self.risk_manager.daily_max_loss_amount:.2f}"
            f")"
        )


        # MT5

        self.start_mt5()

        self.print_symbol_specs()


        # Telegram

        await self.start_telegram()


# ============================================================
# LOCAL LOGIC TEST
# ============================================================

def local_logic_test():

    print(
        "\n"
        + "=" * 90
    )

    print(
        "LOCAL LOGIC TEST"
    )

    print(
        "=" * 90
    )


    bot = NexusBot()


    # No real execution
    bot.execution.mt5_bridge = None


    test_messages = [

        (
            8755,
            "BUY XAUUSD NOW"
        ),

        (
            8756,
            "AT 4133"
        ),

        (
            8757,
            "TP1 4143 TP2 4153 SL4123"
        ),

        (
            8758,
            "SELL XAUUSD NOW AT 4131 "
            "TP1 4121 TP2 4111 SL 4141"
        ),

        (
            8759,
            "اغلاق صفقه الان علي دخول"
        ),

        (
            8761,
            "BUY XAUUSD NOW AT 4131 "
            "TP1 4141 TP2 4151 SL 4121"
        ),

        (
            8762,
            "تامين التوصية علي سعر الدخول الان"
        ),

        (
            8764,
            "BUY GBPCAD NOW AT 1.88400 "
            "TP1 1.88600 TP2 1.89200 "
            "SL 1.88100"
        ),

        (
            8765,
            "تامين التوصية علي سعر الدخول الان"
        ),

        (
            8766,
            "SELL XAUUSD NOW AT 4131 "
            "TP1 4121 TP2 4111 SL 4141"
        ),

        (
            8767,
            "اغلاق صفقه الان"
        ),

        (
            8769,
            "BUY XAUUSD NOW AT 4133 "
            "TP1 4143 TP2 4153 SL 4123"
        ),

        (
            8770,
            "اغلاق صفقه الان علي دخول"
        ),

        (
            8772,
            "SELL XAUUSD NOW AT 4132 "
            "TP1 4122 TP2 4112 SL 4142"
        ),

        (
            8773,
            "تامين التوصية علي سعر الدخول الان"
        ),

        (
            8774,
            "ضربت تامين"
        ),

        (
            8776,
            "BUY XAUUSD NOW AT 4132 "
            "TP1 4142 TP2 4152 SL 4122"
        ),

        (
            8777,
            "تامين التوصية علي سعر الدخول الان"
        ),

        (
            8778,
            "اغلاق صفقه الان"
        ),

        (
            8780,
            "SELL XAUUSD NOW AT 4134 "
            "TP1 4124 TP2 4114 SL 4144"
        ),

        (
            8781,
            "تامين التوصية علي سعر الدخول الان"
        ),

        (
            8782,
            "ضربت تامين"
        ),

        (
            8784,
            "BUY XAUUSD NOW AT 4134 "
            "TP1 4144 TP2 4154 SL 4124"
        ),

        (
            8785,
            "اغلاق صفقه الان"
        ),

        (
            8787,
            "SELL XAUUSD NOW AT 4131 "
            "TP1 4121 TP2 4111 SL 4141"
        ),

        (
            8788,
            "تامين التوصية علي سعر الدخول الان"
        ),

        (
            8789,
            "ضرب الهدف الاول"
        ),

        (
            8790,
            "حجز نصف الارباح"
        ),

        (
            8791,
            "اغلاق صفقه الان"
        ),

        (
            8793,
            "BUY USDCHF NOW AT 0.83300 "
            "TP1 0.83500 TP2 0.83900 "
            "SL 0.83000"
        ),

        (
            8794,
            "BUY XAUUSD NOW AT 4114 "
            "TP1 4124 TP2 4134 SL 4104"
        ),

        (
            8795,
            "اغلاق صفقه الان علي دخول"
        ),

        (
            8797,
            "BUY XAUUSD NOW AT 4114 "
            "TP1 4124 TP2 4134 SL 4104"
        ),

        (
            8798,
            "تامين التوصية علي سعر الدخول الان"
        ),

        (
            8799,
            "ضرب الهدف الاول"
        ),

        (
            8800,
            "حجز نصف الارباح"
        ),

        (
            8802,
            "SELL NZDJPY NOW AT 88.450 "
            "TP1 88.150 TP2 87.500 "
            "SL 88.750"
        ),
    ]


    for message_id, text in (
        test_messages
    ):

        bot.process_message(

            message_id,

            text

        )


    bot.state_manager \
        .print_summary()


    print(
        "\n"
        + "=" * 90
    )

    print(
        "RISK MANAGER"
    )

    print(
        "=" * 90
    )

    print(
        bot.risk_manager.summary()
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        # ------------------------------------------
        # Logic test
        # ------------------------------------------

        if (
            len(sys.argv) > 1
            and
            sys.argv[1].lower()
            ==
            "test"
        ):

            local_logic_test()

            raise SystemExit(0)


        # ------------------------------------------
        # Dependencies
        # ------------------------------------------

        if mt5 is None:

            print(
                "MetaTrader5 is not installed."
            )

            print(
                "Run:"
            )

            print(
                "python -m pip install MetaTrader5"
            )

            raise SystemExit(1)


        if TelegramClient is None:

            print(
                "Telethon is not installed."
            )

            print(
                "Run:"
            )

            print(
                "python -m pip install telethon"
            )

            raise SystemExit(1)


        # ------------------------------------------
        # Start Bot
        # ------------------------------------------

        bot = NexusBot()


        asyncio.run(
            bot.run()
        )


    except KeyboardInterrupt:

        print(
            "\nBot stopped."
        )


    except Exception as exc:

        print(
            f"\nFATAL ERROR:\n{exc}"
        )


    finally:

        if mt5 is not None:

            try:

                mt5.shutdown()

            except Exception:
                pass