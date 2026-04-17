#!/usr/bin/env python3
"""
Facebook Messenger -> Telegram receipt forwarder.

Core workflow
1. FB user sends a transfer screenshot to the page inbox.
2. The bot stores the screenshot and analyzes it.
3. Page admin replies to that user with an approval text such as "DONE"
   or with the known approval image.
4. The bot detects the approval and forwards the stored receipt to Telegram.

Deployment notes
- Expose Flask with a public /webhook endpoint.
- Configure the process as a web process (for example via gunicorn).
- All secrets must be provided through environment variables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import Flask, jsonify, request
from PIL import Image

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


@dataclass(frozen=True)
class Config:
    fb_page_id: str = os.environ.get("FB_PAGE_ID", "").strip()
    fb_app_secret: str = os.environ.get("FB_APP_SECRET", "").strip()
    fb_page_access_token: str = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
    fb_verify_token: str = os.environ.get("FB_VERIFY_TOKEN", "").strip()

    tg_bot_token: str = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_baht_group: str = os.environ.get("TG_BAHT_GROUP", "").strip()
    tg_kyat_group: str = os.environ.get("TG_KYAT_GROUP", "").strip()

    openai_model: str = os.environ.get("OPENAI_VISION_MODEL", "gpt-4.1-mini").strip()
    approval_image_path: str = os.environ.get(
        "APPROVAL_IMAGE_PATH",
        os.path.join(BASE_DIR, "photo_AQADhg9rG3Oo0FZ9.jpg"),
    ).strip()
    db_path: str = os.environ.get("FB_BOT_DB_PATH", os.path.join(DATA_DIR, "fb_to_tg_bot.db")).strip()
    request_timeout: int = int(os.environ.get("REQUEST_TIMEOUT", "30"))


CONFIG = Config()
FB_GRAPH_API = "https://graph.facebook.com/v22.0"
TG_API_BASE = f"https://api.telegram.org/bot{CONFIG.tg_bot_token}" if CONFIG.tg_bot_token else ""
ADMIN_APPROVE_KEYWORDS = [
    "ok",
    "done",
    "approve",
    "approved",
    "okay",
    "yes",
    "ပြီး",
    "ပြီ",
    "ရပြီ",
    "မှန်ကန်ပါစေ",
]

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
    stream=sys.stdout,
)
logger = logging.getLogger("fb_to_tg_bot")


class PendingSlipStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_slips (
                    message_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT,
                    message_text TEXT,
                    image_url TEXT,
                    image_bytes BLOB NOT NULL,
                    currency TEXT,
                    bank_name TEXT,
                    amount TEXT,
                    sender_account_name TEXT,
                    receiver_account_name TEXT,
                    transfer_datetime TEXT,
                    reference_id TEXT,
                    analysis_summary TEXT,
                    target_group TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )

    def has_processed_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return bool(row)

    def mark_processed_message(self, message_id: str) -> None:
        if not message_id:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
                (message_id, datetime.utcnow().isoformat()),
            )

    def save_pending_slip(self, record: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_slips (
                    message_id, sender_id, sender_name, message_text, image_url, image_bytes,
                    currency, bank_name, amount, sender_account_name, receiver_account_name,
                    transfer_datetime, reference_id, analysis_summary, target_group, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["message_id"],
                    record["sender_id"],
                    record.get("sender_name", "Unknown"),
                    record.get("message_text", ""),
                    record.get("image_url", ""),
                    record["image_bytes"],
                    record.get("currency", "unknown"),
                    record.get("bank_name", ""),
                    record.get("amount", ""),
                    record.get("sender_account_name", ""),
                    record.get("receiver_account_name", ""),
                    record.get("transfer_datetime", ""),
                    record.get("reference_id", ""),
                    record.get("analysis_summary", ""),
                    record.get("target_group", ""),
                    record.get("created_at", datetime.utcnow().isoformat()),
                ),
            )

    def get_latest_for_user(self, sender_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pending_slips
                WHERE sender_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (sender_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_any(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_slips ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def delete_pending_slip(self, message_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_slips WHERE message_id = ?", (message_id,))

    def count_pending(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM pending_slips").fetchone()
        return int(row[0]) if row else 0


store = PendingSlipStore(CONFIG.db_path)
app = Flask(__name__)
http = requests.Session()
reference_hash_cache: Optional[str] = None


BAHT_KEYWORDS = [
    "baht",
    "บาท",
    "฿",
    "thb",
    "scb",
    "siam commercial",
    "promptpay",
    "transfer",
    "โอน",
]
KYAT_KEYWORDS = [
    "kyat",
    "ကျပ်",
    "mmk",
    "wave",
    "kbz",
    "aya",
    "cb bank",
    "myanmar",
]


def missing_required_env() -> list[str]:
    missing = []
    required = {
        "FB_PAGE_ACCESS_TOKEN": CONFIG.fb_page_access_token,
        "FB_VERIFY_TOKEN": CONFIG.fb_verify_token,
        "TG_BOT_TOKEN": CONFIG.tg_bot_token,
        "TG_BAHT_GROUP": CONFIG.tg_baht_group,
    }
    for key, value in required.items():
        if not value:
            missing.append(key)
    return missing


def verify_fb_signature(raw_body: bytes) -> bool:
    if not CONFIG.fb_app_secret:
        logger.warning("FB_APP_SECRET not configured; signature verification skipped")
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        CONFIG.fb_app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    provided = signature[len("sha256="):]
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def detect_currency_from_text(text: str) -> str:
    if not text:
        return "unknown"
    text_lower = text.lower()
    baht_score = sum(1 for kw in BAHT_KEYWORDS if kw in text_lower)
    kyat_score = sum(1 for kw in KYAT_KEYWORDS if kw in text_lower)
    if baht_score > kyat_score and baht_score > 0:
        return "baht"
    if kyat_score > baht_score and kyat_score > 0:
        return "kyat"
    return "unknown"


def detect_currency_from_image(image_bytes: bytes) -> str:
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        top_height = max(1, int(image.height * 0.35))
        pixels = list(image.crop((0, 0, image.width, top_height)).getdata())
        yellow_count = 0
        blue_count = 0
        colored = 0
        for r, g, b in pixels:
            brightness = (r + g + b) / 3
            if brightness > 238 or brightness < 15:
                continue
            colored += 1
            if r > 180 and g > 160 and b < 120 and r > b + 80:
                yellow_count += 1
            elif b > 130 and r < 130 and b > r + 40:
                blue_count += 1
        if colored == 0:
            return "unknown"
        if yellow_count / colored > 0.15 or blue_count / colored > 0.10:
            return "kyat"
        if yellow_count / colored < 0.05 and blue_count / colored < 0.05:
            return "baht"
    except Exception as exc:
        logger.warning("Currency image heuristic failed: %s", exc)
    return "unknown"


def average_hash_from_bytes(image_bytes: bytes) -> Optional[str]:
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("L").resize((8, 8))
        pixels = list(image.getdata())
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if pixel >= avg else "0" for pixel in pixels)
        return f"{int(bits, 2):016x}"
    except Exception as exc:
        logger.warning("Approval image hash failed: %s", exc)
        return None


def load_reference_hash() -> Optional[str]:
    global reference_hash_cache
    if reference_hash_cache is not None:
        return reference_hash_cache
    if not os.path.exists(CONFIG.approval_image_path):
        logger.warning("Approval reference image not found at %s", CONFIG.approval_image_path)
        return None
    with open(CONFIG.approval_image_path, "rb") as handle:
        reference_hash_cache = average_hash_from_bytes(handle.read())
    return reference_hash_cache


def hamming_distance(hash_a: str, hash_b: str) -> int:
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


def contains_approve_keyword(text: str) -> bool:
    text_lower = (text or "").strip().lower()
    return any(keyword in text_lower for keyword in ADMIN_APPROVE_KEYWORDS)


def matches_approval_image(image_bytes: bytes) -> bool:
    reference_hash = load_reference_hash()
    candidate_hash = average_hash_from_bytes(image_bytes)
    if not reference_hash or not candidate_hash:
        return False
    distance = hamming_distance(reference_hash, candidate_hash)
    logger.info("Approval image hash distance: %s", distance)
    return distance <= int(os.environ.get("APPROVAL_HASH_DISTANCE", "8"))


def build_openai_client() -> Optional[OpenAI]:
    if OpenAI is None:
        return None
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    return OpenAI()


def analyze_slip_with_vision(image_bytes: bytes, message_text: str = "") -> Dict[str, Any]:
    fallback_currency = detect_currency_from_text(message_text)
    if fallback_currency == "unknown":
        fallback_currency = detect_currency_from_image(image_bytes)

    fallback_result = {
        "is_bank_slip": fallback_currency == "baht",
        "bank_name": "SCB" if fallback_currency == "baht" else "",
        "currency": fallback_currency if fallback_currency != "unknown" else "baht",
        "amount": "",
        "sender_name": "",
        "receiver_name": "",
        "transfer_datetime": "",
        "reference_id": "",
        "confidence": 0.0,
        "raw_summary": "Vision API unavailable; using fallback detection.",
    }

    client = build_openai_client()
    if client is None:
        return fallback_result

    try:
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        prompt = (
            "Analyze this payment screenshot. Return JSON only with keys: "
            "is_bank_slip (boolean), bank_name (string), currency (string using baht/kyat/unknown), "
            "amount (string), sender_name (string), receiver_name (string), transfer_datetime (string), "
            "reference_id (string), confidence (number 0..1), raw_summary (string). "
            "If this looks like an SCB or Thai transfer slip, extract the amount and the sender/receiver names carefully. "
            "If a field is unclear, use an empty string. Message context: "
            f"{message_text or 'none'}"
        )
        response = client.chat.completions.create(
            model=CONFIG.openai_model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You extract bank transfer data from screenshots and reply with strict JSON.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                        },
                    ],
                },
            ],
            max_tokens=500,
            timeout=45,
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        result = {
            "is_bank_slip": bool(parsed.get("is_bank_slip", False)),
            "bank_name": str(parsed.get("bank_name", "")).strip(),
            "currency": str(parsed.get("currency", fallback_result["currency"])).strip().lower(),
            "amount": str(parsed.get("amount", "")).strip(),
            "sender_name": str(parsed.get("sender_name", "")).strip(),
            "receiver_name": str(parsed.get("receiver_name", "")).strip(),
            "transfer_datetime": str(parsed.get("transfer_datetime", "")).strip(),
            "reference_id": str(parsed.get("reference_id", "")).strip(),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "raw_summary": str(parsed.get("raw_summary", "")).strip(),
        }
        if result["currency"] not in {"baht", "kyat", "unknown"}:
            result["currency"] = fallback_result["currency"]
        if result["currency"] == "unknown":
            result["currency"] = fallback_result["currency"]
        return result
    except Exception as exc:
        logger.warning("Vision analysis failed, using fallback: %s", exc)
        return fallback_result


def get_fb_user_profile(sender_id: str) -> Dict[str, str]:
    profile = {"id": sender_id, "name": "Unknown"}
    if not CONFIG.fb_page_access_token:
        return profile
    try:
        resp = http.get(
            f"{FB_GRAPH_API}/{sender_id}",
            params={"fields": "name", "access_token": CONFIG.fb_page_access_token},
            timeout=CONFIG.request_timeout,
        )
        if resp.ok:
            data = resp.json()
            profile["name"] = data.get("name", profile["name"])
        else:
            logger.warning("FB profile lookup failed: %s %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.warning("FB profile lookup error: %s", exc)
    return profile


def download_fb_image(image_url: str) -> Optional[bytes]:
    if not image_url:
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    if CONFIG.fb_page_access_token:
        headers["Authorization"] = f"Bearer {CONFIG.fb_page_access_token}"
    try:
        resp = http.get(image_url, headers=headers, timeout=CONFIG.request_timeout)
        if resp.ok:
            return resp.content
        logger.warning("FB image download failed: %s %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        logger.warning("FB image download error: %s", exc)
    return None


def send_fb_reply(recipient_id: str, text: str) -> bool:
    if not CONFIG.fb_page_access_token:
        logger.warning("Cannot send FB reply because FB_PAGE_ACCESS_TOKEN is missing")
        return False
    try:
        resp = http.post(
            f"{FB_GRAPH_API}/me/messages",
            params={"access_token": CONFIG.fb_page_access_token},
            json={"recipient": {"id": recipient_id}, "message": {"text": text}},
            timeout=CONFIG.request_timeout,
        )
        if not resp.ok:
            logger.warning("FB reply failed: %s %s", resp.status_code, resp.text[:500])
            return False
        return True
    except Exception as exc:
        logger.warning("FB reply error: %s", exc)
        return False


def send_telegram_photo(chat_id: str, photo_bytes: bytes, caption: str) -> bool:
    if not CONFIG.tg_bot_token:
        logger.warning("Cannot send Telegram photo because TG_BOT_TOKEN is missing")
        return False
    try:
        resp = http.post(
            f"{TG_API_BASE}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("receipt.jpg", photo_bytes, "image/jpeg")},
            timeout=CONFIG.request_timeout,
        )
        payload = resp.json()
        if payload.get("ok"):
            return True
        logger.warning("Telegram API rejected photo: %s", payload)
    except Exception as exc:
        logger.warning("Telegram photo send error: %s", exc)
    return False


def build_caption(record: Dict[str, Any]) -> str:
    currency = (record.get("currency") or "baht").lower()
    currency_label = "ထိုင်းဘတ် (฿)" if currency == "baht" else "မြန်မာကျပ် (K)"

    lines = [
        "📋 <b>ငွေပေးချေမှု ပြေစာ</b>",
        f"👤 ပို့သူ: {record.get('sender_name') or 'Unknown'}",
        f"💰 ငွေကြေး: {currency_label}",
    ]

    if record.get("bank_name"):
        lines.append(f"🏦 ဘဏ်: {record['bank_name']}")
    if record.get("amount"):
        lines.append(f"💵 ငွေပမာဏ: {record['amount']}")
    if record.get("sender_account_name"):
        lines.append(f"↗️ လွှဲသူ: {record['sender_account_name']}")
    if record.get("receiver_account_name"):
        lines.append(f"↘️ လက်ခံသူ: {record['receiver_account_name']}")
    if record.get("transfer_datetime"):
        lines.append(f"🕒 လွှဲချိန်: {record['transfer_datetime']}")
    if record.get("reference_id"):
        lines.append(f"🧾 Ref: {record['reference_id']}")
    if record.get("message_text"):
        lines.append(f"📝 FB မက်ဆေ့ချ်: {record['message_text']}")
    if record.get("analysis_summary"):
        lines.append(f"🤖 Analysis: {record['analysis_summary']}")
    lines.append(f"📥 သိမ်းဆည်းချိန်: {record.get('created_at', '')}")
    return "\n".join(lines)


def choose_target_group(currency: str) -> str:
    return CONFIG.tg_kyat_group if currency == "kyat" else CONFIG.tg_baht_group


def store_user_slip(message_id: str, sender_id: str, sender_name: str, message_text: str, image_url: str) -> bool:
    image_bytes = download_fb_image(image_url)
    if not image_bytes:
        return False

    analysis = analyze_slip_with_vision(image_bytes, message_text)
    currency = analysis.get("currency") or detect_currency_from_text(message_text)
    if currency == "unknown":
        currency = detect_currency_from_image(image_bytes)
    if currency == "unknown":
        currency = "baht"

    record = {
        "message_id": message_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message_text": message_text,
        "image_url": image_url,
        "image_bytes": image_bytes,
        "currency": currency,
        "bank_name": analysis.get("bank_name", ""),
        "amount": analysis.get("amount", ""),
        "sender_account_name": analysis.get("sender_name", ""),
        "receiver_account_name": analysis.get("receiver_name", ""),
        "transfer_datetime": analysis.get("transfer_datetime", ""),
        "reference_id": analysis.get("reference_id", ""),
        "analysis_summary": analysis.get("raw_summary", ""),
        "target_group": choose_target_group(currency),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    store.save_pending_slip(record)
    logger.info(
        "Stored pending slip message_id=%s sender=%s currency=%s amount=%s",
        message_id,
        sender_id,
        currency,
        record.get("amount", ""),
    )
    return True


def forward_pending_slip(record: Dict[str, Any]) -> bool:
    success = send_telegram_photo(
        record.get("target_group") or choose_target_group(record.get("currency", "baht")),
        record["image_bytes"],
        build_caption(record),
    )
    if not success:
        return False

    store.delete_pending_slip(record["message_id"])
    send_fb_reply(
        record["sender_id"],
        "✅ ပြေစာရပါပြီ။\n⏳ ယူနစ်ဖြည့်ပေးနေပါပြီ၊ ခဏစောင့်ပါ။\n🙏 ကျေးဇူးတင်ပါတယ်ခင်ဗျာ။",
    )
    return True


def message_contains_approval(event: Dict[str, Any]) -> bool:
    message = event.get("message", {})
    text = (message.get("text") or "").strip()
    if contains_approve_keyword(text):
        return True

    for attachment in message.get("attachments", []):
        if attachment.get("type") != "image":
            continue
        image_url = attachment.get("payload", {}).get("url", "")
        image_bytes = download_fb_image(image_url)
        if image_bytes and matches_approval_image(image_bytes):
            return True
    return False


def process_messaging_event(event: Dict[str, Any]) -> None:
    sender_id = event.get("sender", {}).get("id", "")
    recipient_id = event.get("recipient", {}).get("id", "")
    message = event.get("message") or {}
    message_id = str(message.get("mid", "") or event.get("timestamp", ""))

    if not message_id:
        logger.info("Skipping event without message id")
        return
    if store.has_processed_message(message_id):
        logger.info("Skipping already-processed message %s", message_id)
        return

    text = (message.get("text") or "").strip()
    attachments = message.get("attachments", [])
    is_page_message = bool(message.get("is_echo")) or (CONFIG.fb_page_id and sender_id == CONFIG.fb_page_id)

    logger.info(
        "Processing message_id=%s sender=%s recipient=%s is_page_message=%s attachments=%s text=%r",
        message_id,
        sender_id,
        recipient_id,
        is_page_message,
        len(attachments),
        text[:120],
    )

    if is_page_message:
        if message_contains_approval(event):
            pending = None
            if recipient_id:
                pending = store.get_latest_for_user(recipient_id)
            if not pending:
                pending = store.get_latest_any()
            if pending:
                if forward_pending_slip(pending):
                    logger.info("Forwarded pending slip %s after admin approval", pending["message_id"])
                else:
                    logger.warning("Failed to forward pending slip %s", pending["message_id"])
            else:
                logger.info("Approval detected but no pending slip was available")
        else:
            logger.info("Page/admin message did not match approval rule")
        store.mark_processed_message(message_id)
        return

    if not attachments:
        store.mark_processed_message(message_id)
        return

    profile = get_fb_user_profile(sender_id)
    sender_name = profile.get("name", "Unknown")
    stored = False
    for attachment in attachments:
        if attachment.get("type") != "image":
            continue
        image_url = attachment.get("payload", {}).get("url", "")
        if not image_url:
            continue
        stored = store_user_slip(
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            message_text=text,
            image_url=image_url,
        )
        if stored:
            break

    if not stored:
        logger.info("No supported image attachment was stored for message %s", message_id)
    store.mark_processed_message(message_id)


@app.get("/")
@app.get("/healthz")
def healthcheck():
    return jsonify(
        {
            "status": "ok",
            "service": "fb_to_tg_bot",
            "pending_slips": store.count_pending(),
            "missing_env": missing_required_env(),
            "approval_reference_present": os.path.exists(CONFIG.approval_image_path),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    )


@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == CONFIG.fb_verify_token:
        logger.info("Facebook webhook verified successfully")
        return challenge or "", 200
    logger.warning("Facebook webhook verification failed")
    return "Forbidden", 403


@app.post("/webhook")
def webhook_receive():
    raw_body = request.get_data() or b""
    if not verify_fb_signature(raw_body):
        logger.warning("Rejected webhook with invalid signature")
        return "Forbidden", 403

    body = request.get_json(silent=True) or {}
    if body.get("object") != "page":
        return "OK", 200

    try:
        for entry in body.get("entry", []):
            for event in entry.get("messaging", []):
                process_messaging_event(event)
    except Exception as exc:
        logger.exception("Webhook processing error: %s", exc)
    return "OK", 200


def main() -> None:
    logger.info("Starting fb_to_tg_bot")
    logger.info("Database: %s", CONFIG.db_path)
    logger.info("Missing env: %s", missing_required_env())
    load_reference_hash()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
