#!/usr/bin/env python3
"""
Facebook Messenger → Telegram Forwarder Bot
============================================
Receives payment slip images from Facebook Page Messenger via webhook,
detects whether the slip is Thai Baht (ဘတ်) or Myanmar Kyat (ကျပ်),
and forwards the image with sender info to the appropriate Telegram group.

All logs and responses are in Myanmar (Burmese) language.
Compatible with Python 3.11 and Railway deployment.
"""

import os
import sys
import json
import logging
import re
import tempfile
from datetime import datetime

import requests
from flask import Flask, request, jsonify

# =============================================================================
# Configuration
# =============================================================================

# Facebook credentials
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "100089299923143")
FB_APP_ID = os.environ.get("FB_APP_ID", "2471500943307447")
FB_APP_SECRET = os.environ.get("FB_APP_SECRET", "a98d5453e4cafc4d7e7139bd7de6c72a")
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
FB_VERIFY_TOKEN = os.environ.get("FB_VERIFY_TOKEN", "PTTFBBot_verify_2024_secure")

# Telegram credentials
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8744118866:AAGD_QJZxMTkMgHdDFbuSZy8zUZpf9d9ris")
TG_BAHT_GROUP = os.environ.get("TG_BAHT_GROUP", "@ptttbath")
TG_KYAT_GROUP = os.environ.get("TG_KYAT_GROUP", "")  # Set via env or use chat_id

# Telegram API base URL
TG_API_BASE = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Facebook Graph API base URL
FB_GRAPH_API = "https://graph.facebook.com/v18.0"

# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("FBtoTG")

# =============================================================================
# Myanmar language strings
# =============================================================================

MY = {
    "bot_start": "🤖 Facebook → Telegram ဘော့ စတင်ပါပြီ",
    "webhook_verified": "✅ Webhook အတည်ပြုပြီးပါပြီ",
    "msg_received": "📩 Facebook မှ မက်ဆေ့ချ် လက်ခံရရှိပါပြီ",
    "image_received": "🖼️ ဓာတ်ပုံ လက်ခံရရှိပါပြီ - ပို့သူ: {sender}",
    "forwarded_baht": "✅ ဘတ် Telegram အုပ်စုသို့ ပို့ပြီးပါပြီ",
    "forwarded_kyat": "✅ ကျပ် Telegram အုပ်စုသို့ ပို့ပြီးပါပြီ",
    "forwarded_both": "✅ Telegram အုပ်စုနှစ်ခုလုံးသို့ ပို့ပြီးပါပြီ",
    "forward_failed": "❌ Telegram သို့ ပို့ရာတွင် အမှားဖြစ်ပါသည်: {error}",
    "detection_baht": "💰 ထိုင်းဘတ် (฿) စလစ်အဖြစ် သိရှိပါသည်",
    "detection_kyat": "💰 မြန်မာကျပ် (K) စလစ်အဖြစ် သိရှိပါသည်",
    "detection_unknown": "❓ ငွေကြေးအမျိုးအစား မသိရှိပါ - အုပ်စုနှစ်ခုလုံးသို့ ပို့ပါမည်",
    "no_token": "⚠️ FB_PAGE_ACCESS_TOKEN မရှိပါ - Facebook API ကို အသုံးပြု၍မရပါ",
    "sender_info": "👤 ပို့သူ: {name}\n🆔 Facebook ID: {id}\n🕐 အချိန်: {time}",
    "slip_caption": "📋 စလစ်အချက်အလက်:\n👤 ပို့သူ: {name}\n🆔 FB ID: {fb_id}\n💰 ငွေကြေး: {currency}\n🕐 အချိန်: {time}",
    "text_received": "📝 စာသား လက်ခံရရှိပါပြီ - ပို့သူ: {sender}",
    "error_download": "❌ ဓာတ်ပုံ ဒေါင်းလုဒ် အမှားဖြစ်ပါသည်",
    "error_processing": "❌ မက်ဆေ့ချ် လုပ်ဆောင်ရာတွင် အမှားဖြစ်ပါသည်",
    "health_ok": "✅ ဘော့ ကောင်းမွန်စွာ အလုပ်လုပ်နေပါသည်",
}

# =============================================================================
# Currency detection keywords
# =============================================================================

BAHT_KEYWORDS = [
    "baht", "บาท", "฿", "thb", "ဘတ်", "ထိုင်း", "thai",
    "kbank", "scb", "bbl", "ktb", "tmb", "gsb", "bay",
    "kasikorn", "siam commercial", "bangkok bank", "krungsri",
    "promptpay", "พร้อมเพย์", "โอนเงิน", "สำเร็จ",
    "truemoney", "true wallet", "ทรูมันนี่",
]

KYAT_KEYWORDS = [
    "kyat", "ကျပ်", "mmk", "ks", "မြန်မာ", "myanmar",
    "kbz", "cb bank", "aya bank", "yoma bank", "mab",
    "kbzpay", "wave money", "wavepay", "ok dollar", "onepay",
    "ငွေလွှဲ", "ငွေလက်ခံ", "အောင်မြင်ပါသည်",
]


# =============================================================================
# Helper functions
# =============================================================================

def detect_currency(text: str) -> str:
    """
    Detect currency type from text content.
    Returns: 'baht', 'kyat', or 'unknown'
    """
    if not text:
        return "unknown"

    text_lower = text.lower()

    baht_score = sum(1 for kw in BAHT_KEYWORDS if kw.lower() in text_lower)
    kyat_score = sum(1 for kw in KYAT_KEYWORDS if kw.lower() in text_lower)

    if baht_score > kyat_score and baht_score > 0:
        return "baht"
    elif kyat_score > baht_score and kyat_score > 0:
        return "kyat"
    else:
        return "unknown"


def get_fb_user_profile(sender_id: str) -> dict:
    """
    Fetch Facebook user profile information.
    Returns dict with 'name' and 'id'.
    """
    profile = {"name": f"FB User {sender_id}", "id": sender_id}

    if not FB_PAGE_ACCESS_TOKEN:
        logger.warning(MY["no_token"])
        return profile

    try:
        url = f"{FB_GRAPH_API}/{sender_id}"
        params = {
            "fields": "name,first_name,last_name",
            "access_token": FB_PAGE_ACCESS_TOKEN,
        }
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            profile["name"] = data.get("name", profile["name"])
            profile["first_name"] = data.get("first_name", "")
            profile["last_name"] = data.get("last_name", "")
            logger.info(f"FB profile fetched: {profile['name']}")
        else:
            logger.warning(f"FB profile fetch failed: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"FB profile error: {e}")

    return profile


def download_fb_image(image_url: str) -> bytes:
    """
    Download image from Facebook CDN.
    Returns image bytes or None.
    """
    try:
        resp = requests.get(image_url, timeout=30)
        if resp.status_code == 200:
            return resp.content
        else:
            logger.error(f"Image download failed: {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Image download error: {e}")
        return None


def get_fb_attachment_url(attachment_url: str) -> str:
    """
    Get the actual image URL from Facebook attachment.
    If a PAGE_ACCESS_TOKEN is available, use it to get a higher-res version.
    """
    return attachment_url


def send_telegram_photo(chat_id: str, photo_bytes: bytes, caption: str, filename: str = "slip.jpg") -> bool:
    """
    Send a photo to a Telegram chat/group.
    Returns True on success.
    """
    try:
        url = f"{TG_API_BASE}/sendPhoto"
        files = {"photo": (filename, photo_bytes, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        resp = requests.post(url, files=files, data=data, timeout=30)

        if resp.status_code == 200:
            result = resp.json()
            if result.get("ok"):
                logger.info(f"Photo sent to Telegram chat {chat_id}")
                return True
            else:
                logger.error(f"Telegram API error: {result}")
                return False
        else:
            logger.error(f"Telegram send failed: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False


def send_telegram_message(chat_id: str, text: str) -> bool:
    """
    Send a text message to a Telegram chat/group.
    Returns True on success.
    """
    try:
        url = f"{TG_API_BASE}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        resp = requests.post(url, data=data, timeout=30)

        if resp.status_code == 200:
            result = resp.json()
            if result.get("ok"):
                logger.info(f"Message sent to Telegram chat {chat_id}")
                return True
            else:
                logger.error(f"Telegram API error: {result}")
                return False
        else:
            logger.error(f"Telegram send failed: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False


def send_fb_reply(sender_id: str, message_text: str) -> bool:
    """
    Send a reply message back to the Facebook Messenger user.
    Returns True on success.
    """
    if not FB_PAGE_ACCESS_TOKEN:
        logger.warning("Cannot reply to FB - no page access token")
        return False

    try:
        url = f"{FB_GRAPH_API}/me/messages"
        params = {"access_token": FB_PAGE_ACCESS_TOKEN}
        payload = {
            "recipient": {"id": sender_id},
            "message": {"text": message_text},
        }
        resp = requests.post(url, params=params, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"FB reply sent to {sender_id}")
            return True
        else:
            logger.warning(f"FB reply failed: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logger.error(f"FB reply error: {e}")
        return False


def forward_image_to_telegram(image_url: str, sender_profile: dict, caption_text: str = ""):
    """
    Download image from Facebook and forward to appropriate Telegram group(s).
    """
    # Download the image
    image_bytes = download_fb_image(image_url)
    if not image_bytes:
        logger.error(MY["error_download"])
        return False

    # Detect currency from any caption text
    currency = detect_currency(caption_text)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build caption for Telegram
    if currency == "baht":
        currency_label = "ထိုင်းဘတ် (฿)"
        logger.info(MY["detection_baht"])
    elif currency == "kyat":
        currency_label = "မြန်မာကျပ် (K)"
        logger.info(MY["detection_kyat"])
    else:
        currency_label = "မသိရှိ (Unknown)"
        logger.info(MY["detection_unknown"])

    tg_caption = MY["slip_caption"].format(
        name=sender_profile.get("name", "Unknown"),
        fb_id=sender_profile.get("id", "Unknown"),
        currency=currency_label,
        time=now_str,
    )

    # Add original caption text if present
    if caption_text:
        tg_caption += f"\n📝 မက်ဆေ့ချ်: {caption_text}"

    # Forward to appropriate group(s)
    success = False

    if currency == "baht":
        # Forward to Baht group only
        if TG_BAHT_GROUP:
            success = send_telegram_photo(TG_BAHT_GROUP, image_bytes, tg_caption)
            if success:
                logger.info(MY["forwarded_baht"])
    elif currency == "kyat":
        # Forward to Kyat group only
        if TG_KYAT_GROUP:
            success = send_telegram_photo(TG_KYAT_GROUP, image_bytes, tg_caption)
            if success:
                logger.info(MY["forwarded_kyat"])
    else:
        # Unknown currency - forward to both groups
        success_baht = False
        success_kyat = False

        if TG_BAHT_GROUP:
            success_baht = send_telegram_photo(TG_BAHT_GROUP, image_bytes, tg_caption)
        if TG_KYAT_GROUP:
            success_kyat = send_telegram_photo(TG_KYAT_GROUP, image_bytes, tg_caption)

        success = success_baht or success_kyat
        if success:
            logger.info(MY["forwarded_both"])

    if not success:
        logger.error(MY["forward_failed"].format(error="No target group or send failed"))

    return success


def forward_text_to_telegram(text: str, sender_profile: dict):
    """
    Forward text messages that may contain slip information to Telegram groups.
    """
    currency = detect_currency(text)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if currency == "baht":
        currency_label = "ထိုင်းဘတ် (฿)"
    elif currency == "kyat":
        currency_label = "မြန်မာကျပ် (K)"
    else:
        currency_label = "မသိရှိ (Unknown)"

    tg_message = (
        f"📝 <b>Facebook မှ စာသားမက်ဆေ့ချ်</b>\n\n"
        f"👤 ပို့သူ: {sender_profile.get('name', 'Unknown')}\n"
        f"🆔 FB ID: {sender_profile.get('id', 'Unknown')}\n"
        f"💰 ငွေကြေး: {currency_label}\n"
        f"🕐 အချိန်: {now_str}\n\n"
        f"📋 မက်ဆေ့ချ်:\n{text}"
    )

    success = False

    if currency == "baht":
        if TG_BAHT_GROUP:
            success = send_telegram_message(TG_BAHT_GROUP, tg_message)
    elif currency == "kyat":
        if TG_KYAT_GROUP:
            success = send_telegram_message(TG_KYAT_GROUP, tg_message)
    else:
        # Forward to both if unknown
        s1 = send_telegram_message(TG_BAHT_GROUP, tg_message) if TG_BAHT_GROUP else False
        s2 = send_telegram_message(TG_KYAT_GROUP, tg_message) if TG_KYAT_GROUP else False
        success = s1 or s2

    return success


# =============================================================================
# Flask Application
# =============================================================================

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    """Health check endpoint"""
    return jsonify({
        "status": "ok",
        "message": MY["health_ok"],
        "bot": "Facebook → Telegram Forwarder",
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """
    Facebook webhook verification endpoint.
    Facebook sends a GET request with hub.mode, hub.verify_token, and hub.challenge.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    logger.info(f"Webhook verification request: mode={mode}, token={token}")

    if mode == "subscribe" and token == FB_VERIFY_TOKEN:
        logger.info(MY["webhook_verified"])
        return challenge, 200
    else:
        logger.warning(f"Webhook verification failed: token mismatch")
        return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    """
    Facebook webhook message receiver.
    Processes incoming messages from Facebook Messenger.
    """
    try:
        body = request.get_json()

        if not body:
            logger.warning("Empty webhook body received")
            return "OK", 200

        logger.info(MY["msg_received"])
        logger.debug(f"Webhook body: {json.dumps(body, indent=2)}")

        # Verify this is a page subscription event
        if body.get("object") != "page":
            logger.warning(f"Unexpected object type: {body.get('object')}")
            return "OK", 200

        # Process each entry
        for entry in body.get("entry", []):
            # Process each messaging event
            for messaging_event in entry.get("messaging", []):
                process_messaging_event(messaging_event)

        return "OK", 200

    except Exception as e:
        logger.error(f"{MY['error_processing']}: {e}")
        return "OK", 200


def process_messaging_event(event: dict):
    """
    Process a single messaging event from Facebook.
    """
    try:
        sender_id = event.get("sender", {}).get("id", "")
        recipient_id = event.get("recipient", {}).get("id", "")

        # Ignore messages sent by the page itself
        if sender_id == FB_PAGE_ID:
            logger.debug("Ignoring message from page itself")
            return

        message = event.get("message", {})
        if not message:
            logger.debug("No message in event")
            return

        # Get sender profile
        sender_profile = get_fb_user_profile(sender_id)
        logger.info(MY["image_received"].format(sender=sender_profile.get("name", sender_id)))

        # Check for attachments (images/photos)
        attachments = message.get("attachments", [])
        message_text = message.get("text", "")

        has_image = False
        for attachment in attachments:
            att_type = attachment.get("type", "")
            payload = attachment.get("payload", {})

            if att_type == "image":
                has_image = True
                image_url = payload.get("url", "")
                if image_url:
                    logger.info(f"Processing image attachment: {image_url[:80]}...")
                    success = forward_image_to_telegram(
                        image_url=image_url,
                        sender_profile=sender_profile,
                        caption_text=message_text,
                    )

                    # Reply to sender on Facebook
                    if success and FB_PAGE_ACCESS_TOKEN:
                        send_fb_reply(
                            sender_id,
                            "✅ စလစ်ဓာတ်ပုံ လက်ခံရရှိပါပြီ။ စစ်ဆေးပေးပါမည်။"
                        )

        # If no image but has text with potential slip info, forward text
        if not has_image and message_text:
            logger.info(MY["text_received"].format(sender=sender_profile.get("name", sender_id)))

            # Check if text contains currency-related keywords
            currency = detect_currency(message_text)
            if currency != "unknown":
                forward_text_to_telegram(message_text, sender_profile)

                if FB_PAGE_ACCESS_TOKEN:
                    send_fb_reply(
                        sender_id,
                        "✅ မက်ဆေ့ချ် လက်ခံရရှိပါပြီ။ စစ်ဆေးပေးပါမည်။"
                    )

    except Exception as e:
        logger.error(f"Error processing messaging event: {e}")


# =============================================================================
# Main entry point
# =============================================================================

def main():
    """Start the Flask server"""
    logger.info(MY["bot_start"])
    logger.info(f"FB Page ID: {FB_PAGE_ID}")
    logger.info(f"TG Baht Group: {TG_BAHT_GROUP}")
    logger.info(f"TG Kyat Group: {TG_KYAT_GROUP or '(not set - use TG_KYAT_GROUP env var)'}")
    logger.info(f"Verify Token: {FB_VERIFY_TOKEN}")

    if not FB_PAGE_ACCESS_TOKEN:
        logger.warning(MY["no_token"])
        logger.warning("Set FB_PAGE_ACCESS_TOKEN environment variable to enable full functionality")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
