#!/usr/bin/env python3
"""
Thai Baht payment slip tracking bot for Telegram.

Features
- Tracks text-based and image-based slip submissions.
- Uses Vision API when available for screenshot parsing.
- Stores records in SQLite.
- Detects duplicate transaction references.
- Uses environment variables instead of hard-coded secrets.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

try:
    from telegram import Update
    from telegram.ext import CallbackContext, CommandHandler, Filters, MessageHandler, Updater
except ImportError:  # pragma: no cover
    from telegram import Update
    from telegram.ext import CommandHandler, MessageHandler, Updater, filters as Filters
    CallbackContext = object


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("tg_slip_bot")


@dataclass(frozen=True)
class Config:
    token: str = os.environ.get("TG_BOT_TOKEN", "").strip()
    special_user: str = os.environ.get("TG_SPECIAL_USER", "").strip().lstrip("@")
    db_path: str = os.environ.get("SLIP_DB_PATH", os.path.join(DATA_DIR, "slip_records.db")).strip()
    openai_model: str = os.environ.get("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()


CONFIG = Config()

MYANMAR_STRINGS = {
    "welcome": "ကြိုဆိုပါသည်။ Thai Baht ငွေလွှဲမှတ်တမ်းစနစ်ဖြစ်ပါသည်။",
    "help_title": "အမိန့်များ",
    "help_text": (
        "/summary - ယနေ့ အကျဉ်းချုပ်\n"
        "/list [YYYY-MM-DD] - နေ့အလိုက်စာရင်း\n"
        "/check [slip_id] - Ref/Slip ID စစ်ရန်\n"
        "/balance - စုစုပေါင်းလက်ကျန်\n"
        "/help - အကူအညီ"
    ),
    "summary_header": "ယနေ့၏ အကျဉ်းချုပ်",
    "summary_in": "စုစုပေါင်းအဝင်: {amount} ဘတ်",
    "summary_out": "စုစုပေါင်းအထုတ်: {amount} ဘတ်",
    "summary_balance": "လက်ကျန်: {amount} ဘတ်",
    "list_header": "{date} အတွက် ငွေလွှဲစာရင်း",
    "list_empty": "ဤနေ့တွင် မှတ်တမ်းမရှိပါ။",
    "list_item": "{time} | {type} | {amount} ဘတ် | ID: {slip_id} | {username}",
    "check_found": "တွေ့ရှိပါသည်။\n{details}",
    "check_not_found": "မတွေ့ရှိပါ။ ID: {slip_id}",
    "balance_current": "လက်ကျန်ငွေ: {amount} ဘတ်",
    "duplicate_alert": "Duplicate slip တွေ့ရှိပါသည်။\nID: {slip_id}\nယခင်အသုံးပြုသူ: {previous_user}\nယခင်အချိန်: {previous_time}",
    "invalid_slip_alert": "Slip အချက်အလက် မပြည့်စုံပါ။ Ref ID သို့မဟုတ် amount ကို ထပ်စစ်ပေးပါ။",
    "slip_recorded": "မှတ်တမ်းတင်ပြီးပါပြီ။\nID: {slip_id}\nအမျိုးအစား: {type}\nငွေပမာဏ: {amount} ဘတ်",
    "error_invalid_date": "နေ့စွဲ format မှားနေပါတယ်။ YYYY-MM-DD အသုံးပြုပါ။",
    "error_invalid_slip_id": "Slip ID / Ref ကို ထည့်ပေးပါ။",
    "error_processing": "လုပ်ဆောင်မှု မအောင်မြင်ပါ။ နောက်တစ်ကြိမ် ထပ်စမ်းပေးပါ။",
    "incoming": "အဝင်",
    "outgoing": "အထုတ်",
}


class SlipDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slip_id TEXT UNIQUE NOT NULL,
                    timestamp TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    amount REAL NOT NULL,
                    transaction_type TEXT NOT NULL,
                    notes TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS duplicate_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slip_id TEXT NOT NULL,
                    first_user_id INTEGER,
                    first_username TEXT,
                    first_timestamp TEXT,
                    duplicate_user_id INTEGER,
                    duplicate_username TEXT,
                    duplicate_timestamp TEXT,
                    alert_time TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def add_transaction(
        self,
        slip_id: str,
        user_id: int,
        username: str,
        amount: float,
        transaction_type: str,
        notes: str = "",
    ) -> Tuple[bool, str]:
        try:
            with self.connect() as conn:
                existing = conn.execute(
                    "SELECT slip_id, timestamp, username, user_id FROM transactions WHERE slip_id = ?",
                    (slip_id,),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        INSERT INTO duplicate_alerts (
                            slip_id, first_user_id, first_username, first_timestamp,
                            duplicate_user_id, duplicate_username, duplicate_timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            slip_id,
                            existing[3],
                            existing[2],
                            existing[1],
                            user_id,
                            username,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    return False, f"duplicate:{slip_id}:{existing[2]}:{existing[1]}"

                conn.execute(
                    """
                    INSERT INTO transactions (
                        slip_id, timestamp, user_id, username, amount, transaction_type, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slip_id,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        user_id,
                        username,
                        amount,
                        transaction_type,
                        notes,
                    ),
                )
            return True, slip_id
        except Exception as exc:
            logger.exception("Error adding transaction: %s", exc)
            return False, f"error:{exc}"

    def get_today_summary(self) -> Dict[str, float]:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with self.connect() as conn:
                incoming = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE DATE(timestamp) = ? AND transaction_type = ?",
                    (today, MYANMAR_STRINGS["incoming"]),
                ).fetchone()[0]
                outgoing = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE DATE(timestamp) = ? AND transaction_type = ?",
                    (today, MYANMAR_STRINGS["outgoing"]),
                ).fetchone()[0]
            return {"incoming": incoming, "outgoing": outgoing, "balance": incoming - outgoing}
        except Exception as exc:
            logger.exception("Error getting summary: %s", exc)
            return {"incoming": 0.0, "outgoing": 0.0, "balance": 0.0}

    def get_transactions_by_date(self, date_str: str) -> List[Dict[str, str]]:
        try:
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT slip_id, timestamp, username, amount, transaction_type
                    FROM transactions WHERE DATE(timestamp) = ? ORDER BY timestamp DESC
                    """,
                    (date_str,),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.exception("Error getting transactions: %s", exc)
            return []

    def check_slip_id(self, slip_id: str) -> Optional[Dict[str, str]]:
        try:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT slip_id, timestamp, username, amount, transaction_type, notes
                    FROM transactions WHERE slip_id = ?
                    """,
                    (slip_id,),
                ).fetchone()
            return dict(row) if row else None
        except Exception as exc:
            logger.exception("Error checking slip id: %s", exc)
            return None

    def get_total_balance(self) -> float:
        try:
            with self.connect() as conn:
                incoming = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE transaction_type = ?",
                    (MYANMAR_STRINGS["incoming"],),
                ).fetchone()[0]
                outgoing = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE transaction_type = ?",
                    (MYANMAR_STRINGS["outgoing"],),
                ).fetchone()[0]
            return float(incoming) - float(outgoing)
        except Exception as exc:
            logger.exception("Error getting total balance: %s", exc)
            return 0.0


class SlipDetector:
    SLIP_ID_PATTERNS = [
        r"(?:slip|ref|reference|id|no\.?|#)\s*:?\s*([A-Z0-9\-]{6,40})",
        r"([A-Z0-9]{8,25})",
    ]
    AMOUNT_PATTERNS = [
        r"(?:amount|total|sum|baht|บาท|THB)\s*:?\s*([0-9]+(?:[.,][0-9]{1,2})?)",
        r"([0-9]+(?:[.,][0-9]{1,2})?)\s*(?:baht|บาท|฿|THB)",
        r"(?:฿|THB)\s*([0-9]+(?:[.,][0-9]{1,2})?)",
    ]

    @staticmethod
    def extract_from_text(text: str) -> Tuple[Optional[str], Optional[float]]:
        if not text:
            return None, None

        slip_id = None
        amount = None
        upper_text = text.upper()

        for pattern in SlipDetector.SLIP_ID_PATTERNS:
            match = re.search(pattern, upper_text, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                if 6 <= len(candidate) <= 40:
                    slip_id = candidate
                    break

        for pattern in SlipDetector.AMOUNT_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                raw = match.group(1).replace(",", "")
                try:
                    value = float(raw)
                    if 0 < value < 10000000:
                        amount = value
                        break
                except ValueError:
                    continue

        return slip_id, amount

    @staticmethod
    def is_valid_slip(slip_id: Optional[str], amount: Optional[float]) -> bool:
        return bool(slip_id) and amount is not None and 0 < amount < 10000000


class VisionSlipAnalyzer:
    def __init__(self, model: str):
        self.model = model
        self.client = OpenAI() if OpenAI is not None and os.environ.get("OPENAI_API_KEY") else None

    def analyze(self, image_bytes: bytes, message_text: str = "") -> Dict[str, str]:
        if self.client is None:
            return {
                "bank_name": "",
                "amount": "",
                "reference_id": self.fallback_image_id(image_bytes),
                "sender_name": "",
                "receiver_name": "",
                "transfer_datetime": "",
                "raw_summary": "Vision API not configured.",
            }

        try:
            encoded = base64.b64encode(image_bytes).decode("utf-8")
            prompt = (
                "Read this Thai bank transfer screenshot and return JSON only with keys: "
                "bank_name, amount, reference_id, sender_name, receiver_name, transfer_datetime, raw_summary. "
                "If a field is unclear, return an empty string. Context text: "
                f"{message_text or 'none'}"
            )
            response = self.client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": "You extract SCB and Thai transfer slip data into strict JSON."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
                        ],
                    },
                ],
                max_tokens=400,
                timeout=45,
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
            ref = str(parsed.get("reference_id", "")).strip() or self.fallback_image_id(image_bytes)
            return {
                "bank_name": str(parsed.get("bank_name", "")).strip(),
                "amount": str(parsed.get("amount", "")).strip(),
                "reference_id": ref,
                "sender_name": str(parsed.get("sender_name", "")).strip(),
                "receiver_name": str(parsed.get("receiver_name", "")).strip(),
                "transfer_datetime": str(parsed.get("transfer_datetime", "")).strip(),
                "raw_summary": str(parsed.get("raw_summary", "")).strip(),
            }
        except Exception as exc:
            logger.warning("Vision analysis failed: %s", exc)
            return {
                "bank_name": "",
                "amount": "",
                "reference_id": self.fallback_image_id(image_bytes),
                "sender_name": "",
                "receiver_name": "",
                "transfer_datetime": "",
                "raw_summary": f"Vision analysis failed: {exc}",
            }

    @staticmethod
    def fallback_image_id(image_bytes: bytes) -> str:
        return "IMG-" + hashlib.sha1(image_bytes).hexdigest()[:12].upper()


class TelegramSlipBot:
    def __init__(self, config: Config):
        self.config = config
        self.db = SlipDatabase(config.db_path)
        self.detector = SlipDetector()
        self.vision = VisionSlipAnalyzer(config.openai_model)

    def _transaction_type(self, username: str) -> str:
        normalized = (username or "").lstrip("@")
        if self.config.special_user and normalized == self.config.special_user:
            return MYANMAR_STRINGS["outgoing"]
        return MYANMAR_STRINGS["incoming"]

    def start(self, update: Update, context: CallbackContext) -> None:
        update.message.reply_text(f"{MYANMAR_STRINGS['welcome']}\n\n{MYANMAR_STRINGS['help_text']}")

    def help_command(self, update: Update, context: CallbackContext) -> None:
        update.message.reply_text(f"{MYANMAR_STRINGS['help_title']}\n\n{MYANMAR_STRINGS['help_text']}")

    def summary_command(self, update: Update, context: CallbackContext) -> None:
        summary = self.db.get_today_summary()
        text = (
            f"{MYANMAR_STRINGS['summary_header']}\n\n"
            f"{MYANMAR_STRINGS['summary_in'].format(amount=summary['incoming'])}\n"
            f"{MYANMAR_STRINGS['summary_out'].format(amount=summary['outgoing'])}\n"
            f"{MYANMAR_STRINGS['summary_balance'].format(amount=summary['balance'])}"
        )
        update.message.reply_text(text)

    def list_command(self, update: Update, context: CallbackContext) -> None:
        if context.args:
            date_str = context.args[0]
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                update.message.reply_text(MYANMAR_STRINGS["error_invalid_date"])
                return
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")

        rows = self.db.get_transactions_by_date(date_str)
        if not rows:
            update.message.reply_text(f"{MYANMAR_STRINGS['list_header'].format(date=date_str)}\n\n{MYANMAR_STRINGS['list_empty']}")
            return

        lines = [MYANMAR_STRINGS["list_header"].format(date=date_str), ""]
        for row in rows:
            time_str = str(row["timestamp"]).split(" ")[-1]
            lines.append(
                MYANMAR_STRINGS["list_item"].format(
                    time=time_str,
                    type=row["transaction_type"],
                    amount=row["amount"],
                    slip_id=row["slip_id"],
                    username=row["username"] or "Unknown",
                )
            )
        update.message.reply_text("\n".join(lines))

    def check_command(self, update: Update, context: CallbackContext) -> None:
        if not context.args:
            update.message.reply_text(MYANMAR_STRINGS["error_invalid_slip_id"])
            return
        slip_id = context.args[0]
        record = self.db.check_slip_id(slip_id)
        if not record:
            update.message.reply_text(MYANMAR_STRINGS["check_not_found"].format(slip_id=slip_id))
            return
        details = (
            f"အမျိုးအစား: {record['transaction_type']}\n"
            f"ငွေပမာဏ: {record['amount']} ဘတ်\n"
            f"အသုံးပြုသူ: {record['username']}\n"
            f"အချိန်: {record['timestamp']}\n"
            f"မှတ်ချက်: {record.get('notes') or '-'}"
        )
        update.message.reply_text(MYANMAR_STRINGS["check_found"].format(details=details))

    def balance_command(self, update: Update, context: CallbackContext) -> None:
        update.message.reply_text(MYANMAR_STRINGS["balance_current"].format(amount=self.db.get_total_balance()))

    def extract_photo_bytes(self, update: Update) -> Optional[bytes]:
        try:
            message = update.message
            if message.photo:
                tg_file = message.photo[-1].get_file()
                buffer = io.BytesIO()
                tg_file.download(out=buffer)
                return buffer.getvalue()
            if message.document and (message.document.mime_type or "").startswith("image/"):
                tg_file = message.document.get_file()
                buffer = io.BytesIO()
                tg_file.download(out=buffer)
                return buffer.getvalue()
        except Exception as exc:
            logger.warning("Failed to download Telegram image: %s", exc)
        return None

    def handle_message(self, update: Update, context: CallbackContext) -> None:
        message = update.message
        if not message:
            return

        sender_username = message.from_user.username or f"user_{message.from_user.id}"
        transaction_type = self._transaction_type(sender_username)
        text = (message.caption or message.text or "").strip()

        slip_id = None
        amount = None
        notes = []

        if text:
            slip_id, amount = self.detector.extract_from_text(text)

        if (slip_id is None or amount is None) and (message.photo or message.document):
            image_bytes = self.extract_photo_bytes(update)
            if image_bytes:
                vision = self.vision.analyze(image_bytes, text)
                if not slip_id:
                    slip_id = vision.get("reference_id")
                if amount is None and vision.get("amount"):
                    try:
                        amount = float(str(vision["amount"]).replace(",", ""))
                    except ValueError:
                        amount = None
                if vision.get("bank_name"):
                    notes.append(f"bank={vision['bank_name']}")
                if vision.get("sender_name"):
                    notes.append(f"sender={vision['sender_name']}")
                if vision.get("receiver_name"):
                    notes.append(f"receiver={vision['receiver_name']}")
                if vision.get("transfer_datetime"):
                    notes.append(f"datetime={vision['transfer_datetime']}")
                if vision.get("raw_summary"):
                    notes.append(f"summary={vision['raw_summary']}")

        if not self.detector.is_valid_slip(slip_id, amount):
            if slip_id or amount or message.photo or message.document:
                update.message.reply_text(MYANMAR_STRINGS["invalid_slip_alert"])
            return

        success, result = self.db.add_transaction(
            slip_id=slip_id,
            user_id=message.from_user.id,
            username=sender_username,
            amount=amount,
            transaction_type=transaction_type,
            notes=" | ".join(notes),
        )
        if success:
            update.message.reply_text(
                MYANMAR_STRINGS["slip_recorded"].format(
                    slip_id=slip_id,
                    type=transaction_type,
                    amount=amount,
                )
            )
            return

        if result.startswith("duplicate:"):
            _, dup_id, previous_user, previous_time = result.split(":", 3)
            update.message.reply_text(
                MYANMAR_STRINGS["duplicate_alert"].format(
                    slip_id=dup_id,
                    previous_user=previous_user,
                    previous_time=previous_time,
                )
            )
            return

        update.message.reply_text(MYANMAR_STRINGS["error_processing"])

    def run(self) -> None:
        if not self.config.token:
            raise RuntimeError("TG_BOT_TOKEN is required for tg_slip_bot.py")

        updater = Updater(self.config.token, use_context=True)
        dispatcher = updater.dispatcher
        dispatcher.add_handler(CommandHandler("start", self.start))
        dispatcher.add_handler(CommandHandler("help", self.help_command))
        dispatcher.add_handler(CommandHandler("summary", self.summary_command))
        dispatcher.add_handler(CommandHandler("list", self.list_command))
        dispatcher.add_handler(CommandHandler("check", self.check_command))
        dispatcher.add_handler(CommandHandler("balance", self.balance_command))
        dispatcher.add_handler(MessageHandler(Filters.text | Filters.photo | Filters.document, self.handle_message))

        logger.info("tg_slip_bot started. db=%s", self.config.db_path)
        updater.start_polling(drop_pending_updates=True)
        updater.idle()


def main() -> None:
    bot = TelegramSlipBot(CONFIG)
    bot.run()


if __name__ == "__main__":
    main()
