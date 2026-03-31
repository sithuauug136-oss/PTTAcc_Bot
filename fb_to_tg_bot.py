#!/usr/bin/env python3
"""
Facebook Messenger → Telegram Forwarder Bot
============================================
Workflow:
1. User sends slip image to FB Page
2. Bot stores the slip (does NOT forward yet)
3. Page Admin reacts (like/love/etc) to the message
4. Bot forwards slip to correct TG group (kyat/baht)
5. Bot replies to FB user: "✅ ပြေစာရပါပြီ။ ⏳ ယူနစ်ဖြည့်ပေးနေပါပြီ၊ ခဏစောင့်ပါ။ 🙏 ကျေးဇူးတင်ပါတယ်ခင်ဗျာ။"

Currency detection: image color analysis (Wave=yellow, KBZ=blue → kyat, else baht)
"""

import os
import sys
import io
import logging
from datetime import datetime

import requests
from flask import Flask, request, jsonify

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# =============================================================================
# Configuration
# =============================================================================

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "100089299923143")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "a98d5453e4cafc4d7e7139bd7de6c72a")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "PTTFBBot_verify_2024_secure")

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8744118866:AAGD_QJZxMTkMgHdDFbuSZy8zUZpf9d9ris")
TG_BAHT_GROUP = os.environ.get("TG_BAHT_GROUP", "@ptttbath")
TG_KYAT_GROUP = os.environ.get("TG_KYAT_GROUP", "@pttkyats")

TG_API_BASE = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
FB_GRAPH_API = "https://graph.facebook.com/v18.0"

# In-memory store: {message_id: {image_bytes, currency, target_group, caption, sender_id, sender_name}}
pending_slips = {}

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("FBtoTG")

# =============================================================================
# Currency detection
# =============================================================================

BAHT_KEYWORDS = [
    "baht", "บาท", "฿", "thb", "ဘတ်", "ထိုင်း", "thai",
    "kbank", "scb", "bbl", "ktb", "tmb", "gsb", "bay",
    "kasikorn", "siam commercial", "bangkok bank", "krungsri",
    "promptpay", "พร้อมเพย์", "โอนเงิน", "สำเร็จ",
    "truemoney", "true wallet", "ทรูมันนี่",
]

KYAT_KEYWORDS = [
    "kyat", "ကျပ်", "mmk", "မြန်မာ", "myanmar",
    "kbz", "kbzpay", "cb bank", "aya bank", "yoma bank",
    "wave money", "wavepay", "wave", "ok dollar", "onepay",
    "ငွေလွှဲ", "ငွေလက်ခံ", "အောင်မြင်ပါသည်",
]


def detect_currency_from_text(text: str) -> str:
    if not text:
        return "unknown"
    text_lower = text.lower()
    baht_score = sum(1 for kw in BAHT_KEYWORDS if kw.lower() in text_lower)
    kyat_score = sum(1 for kw in KYAT_KEYWORDS if kw.lower() in text_lower)
    if baht_score > kyat_score and baht_score > 0:
        return "baht"
    elif kyat_score > baht_score and kyat_score > 0:
        return "kyat"
    return "unknown"


def detect_currency_from_image(image_bytes: bytes) -> str:
    if not PIL_AVAILABLE:
        return "unknown"
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = img.size
        top_height = int(height * 0.35)
        top_region = img.crop((0, 0, width, top_height))
        pixels = list(top_region.getdata())
        total = len(pixels)
        if total == 0:
            return "unknown"

        yellow_count = 0
        blue_count = 0
        other_count = 0

        for r, g, b in pixels:
            brightness = (r + g + b) / 3
            if brightness > 235 or brightness < 15:
                continue
            if r > 180 and g > 160 and b < 120 and r > b + 80:
                yellow_count += 1
            elif b > 130 and r < 130 and b > r + 40:
                blue_count += 1
            else:
                other_count += 1

        colored = yellow_count + blue_count + other_count
        if colored == 0:
            return "unknown"

        yellow_ratio = yellow_count / colored
        blue_ratio = blue_count / colored

        logger.info(
            f"Color analysis - yellow: {yellow_ratio:.3f} ({yellow_count}), "
            f"blue: {blue_ratio:.3f} ({blue_count}), total colored: {colored}"
        )

        if yellow_ratio > 0.15:
            logger.info("Detected: Myanmar Kyat (Wave Money - yellow)")
            return "kyat"
        if blue_ratio > 0.10:
            logger.info("Detected: Myanmar Kyat (KBZ - blue)")
            return "kyat"
        if yellow_ratio < 0.05 and blue_ratio < 0.05:
            logger.info("Detected: Thai Baht (no yellow/blue dominant)")
            return "baht"
        return "unknown"
    except Exception as e:
        logger.error(f"Image color analysis error: {e}")
        return "unknown"


def detect_currency(image_bytes: bytes, caption_text: str) -> str:
    currency = detect_currency_from_text(caption_text)
    if currency != "unknown":
        return currency
    if image_bytes:
        currency = detect_currency_from_image(image_bytes)
        if currency != "unknown":
            return currency
    logger.info("Currency unknown - defaulting to baht")
    return "baht"


# =============================================================================
# Facebook API helpers
# =============================================================================

def get_fb_user_profile(sender_id: str) -> dict:
    profile = {"name": "Unknown", "id": sender_id}
    if not FB_PAGE_ACCESS_TOKEN:
        return profile
    try:
        url = f"{FB_GRAPH_API}/{sender_id}"
        params = {"fields": "name", "access_token": FB_PAGE_ACCESS_TOKEN}
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "name" in data:
                profile["name"] = data["name"]
                logger.info(f"FB profile fetched: {data['name']}")
    except Exception as e:
        logger.error(f"FB profile error: {e}")
    return profile


def download_fb_image(image_url: str) -> bytes:
    try:
        headers = {"User-Agent": "facebookexternalua"}
        resp = requests.get(image_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.content
        logger.error(f"Image download failed: {resp.status_code}")
    except Exception as e:
        logger.error(f"Image download error: {e}")
    return None


def send_fb_reply(sender_id: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    try:
        url = f"{FB_GRAPH_API}/me/messages"
        params = {"access_token": FB_PAGE_ACCESS_TOKEN}
        payload = {"recipient": {"id": sender_id}, "message": {"text": text}}
        resp = requests.post(url, params=params, json=payload, timeout=10)
        logger.info(f"FB reply sent to {sender_id}: {resp.status_code}")
    except Exception as e:
        logger.error(f"FB reply error: {e}")


# =============================================================================
# Telegram API helpers
# =============================================================================

def send_telegram_photo(chat_id: str, photo_bytes: bytes, caption: str) -> int:
    try:
        url = f"{TG_API_BASE}/sendPhoto"
        files = {"photo": ("slip.jpg", photo_bytes, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        resp = requests.post(url, files=files, data=data, timeout=30)
        result = resp.json()
        if result.get("ok"):
            msg_id = result["result"]["message_id"]
            logger.info(f"Photo sent to {chat_id}, msg_id={msg_id}")
            return msg_id
        else:
            logger.error(f"Telegram error: {result}")
            return 0
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return 0


# =============================================================================
# Main logic
# =============================================================================

def store_pending_slip(message_id: str, image_url: str, sender_profile: dict, caption_text: str, sender_id: str):
    """Download image and store slip pending admin reaction"""
    image_bytes = download_fb_image(image_url)
    if not image_bytes:
        logger.error("Image download failed - cannot store slip")
        return False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    currency = detect_currency(image_bytes, caption_text)
    sender_name = sender_profile.get("name", "Unknown")

    if currency == "kyat":
        currency_label = "မြန်မာကျပ် (K)"
        target_group = TG_KYAT_GROUP
    else:
        currency_label = "ထိုင်းဘတ် (฿)"
        target_group = TG_BAHT_GROUP

    tg_caption = (
        f"📋 <b>ငွေပေးချေမှု ပြေစာ</b>\n\n"
        f"👤 ပို့သူ: {sender_name}\n"
        f"💰 ငွေကြေး: {currency_label}\n"
        f"🕐 အချိန်: {now_str}"
    )
    if caption_text:
        tg_caption += f"\n📝 မက်ဆေ့ချ်: {caption_text}"

    pending_slips[message_id] = {
        "image_bytes": image_bytes,
        "target_group": target_group,
        "caption": tg_caption,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "currency": currency,
        "timestamp": now_str,
    }
    logger.info(f"Slip stored pending admin reaction, msg_id={message_id}, currency={currency}, target={target_group}")
    return True


def forward_pending_slip(message_id: str):
    """Forward a stored slip to TG after admin reaction"""
    slip = pending_slips.pop(message_id, None)
    if not slip:
        logger.warning(f"No pending slip found for msg_id={message_id}")
        return False

    target_group = slip["target_group"]
    image_bytes = slip["image_bytes"]
    caption = slip["caption"]
    sender_id = slip["sender_id"]
    sender_name = slip["sender_name"]

    msg_id = send_telegram_photo(target_group, image_bytes, caption)
    if msg_id:
        logger.info(f"Slip forwarded to {target_group} for {sender_name}")
        # Reply to FB user
        send_fb_reply(
            sender_id,
            "✅ ပြေစာရပါပြီ။\n⏳ ယူနစ်ဖြည့်ပေးနေပါပြီ၊ ခဏစောင့်ပါ။\n🙏 ကျေးဇူးတင်ပါတယ်ခင်ဗျာ။"
        )
        return True
    else:
        logger.error(f"Failed to forward slip to {target_group}")
        return False


# =============================================================================
# Flask Application
# =============================================================================

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "ok",
        "bot": "Facebook to Telegram Forwarder",
        "pil_available": PIL_AVAILABLE,
        "pending_slips": len(pending_slips),
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        logger.info("Webhook verified")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    try:
        body = request.get_json()
        logger.info(f"Webhook body keys: {list(body.keys()) if body else 'empty'}")
        if not body or body.get("object") != "page":
            return "OK", 200
        for entry in body.get("entry", []):
            logger.info(f"Entry keys: {list(entry.keys())}")
            # Handle messaging events
            for event in entry.get("messaging", []):
                process_messaging_event(event)
            # Handle changes (reactions may come here in some API versions)
            for change in entry.get("changes", []):
                logger.info(f"Change field: {change.get('field')}, value keys: {list(change.get('value', {}).keys())}")
                process_change_event(change)
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "OK", 200


def process_change_event(change: dict):
    """Handle changes field events (some reaction events come here)"""
    try:
        field = change.get("field", "")
        value = change.get("value", {})
        logger.info(f"Change event - field: {field}, value: {value}")
    except Exception as e:
        logger.error(f"Change event error: {e}")


def process_messaging_event(event: dict):
    try:
        sender_id = event.get("sender", {}).get("id", "")
        recipient_id = event.get("recipient", {}).get("id", "")

        # --- Handle admin reaction ---
        # When admin reacts to a message, sender is the Page (admin), recipient is the user
        reaction = event.get("reaction", {})
        if reaction:
            reaction_action = reaction.get("action", "")  # "react" or "unreact"
            reacted_msg_id = str(reaction.get("mid", ""))
            logger.info(f"Reaction event: action={reaction_action}, mid={reacted_msg_id}, sender={sender_id}")

            # Process any "react" action - from any sender (admin or page)
            if reaction_action == "react":
                logger.info(f"React from sender={sender_id}, page_id={FB_PAGE_ID}, pending={list(pending_slips.keys())}")
                if reacted_msg_id in pending_slips:
                    forward_pending_slip(reacted_msg_id)
                else:
                    # Try matching without prefix differences
                    matched = None
                    for key in pending_slips:
                        if key.endswith(reacted_msg_id) or reacted_msg_id.endswith(key):
                            matched = key
                            break
                    if matched:
                        forward_pending_slip(matched)
                    else:
                        logger.info(f"Reaction on non-pending message: {reacted_msg_id}, pending keys: {list(pending_slips.keys())}")
            return

        # --- Handle incoming message from user ---
        # Ignore messages sent by the Page itself
        if sender_id == FB_PAGE_ID:
            return

        message = event.get("message", {})
        if not message:
            return

        message_id = str(message.get("mid", ""))
        sender_profile = get_fb_user_profile(sender_id)
        logger.info(f"Message from: {sender_profile.get('name', sender_id)}, mid={message_id}")

        attachments = message.get("attachments", [])
        message_text = message.get("text", "")

        for attachment in attachments:
            if attachment.get("type") == "image":
                image_url = attachment.get("payload", {}).get("url", "")
                if image_url:
                    logger.info(f"Processing image: {image_url[:80]}...")
                    store_pending_slip(
                        message_id=message_id,
                        image_url=image_url,
                        sender_profile=sender_profile,
                        caption_text=message_text,
                        sender_id=sender_id,
                    )

    except Exception as e:
        logger.error(f"Event processing error: {e}")


def main():
    logger.info("Facebook to Telegram Bot started")
    logger.info(f"PIL available: {PIL_AVAILABLE}")
    logger.info(f"Baht group: {TG_BAHT_GROUP}")
    logger.info(f"Kyat group: {TG_KYAT_GROUP or '(not set)'}")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
