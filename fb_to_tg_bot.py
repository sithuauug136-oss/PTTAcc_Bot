#!/usr/bin/env python3
"""
Facebook Messenger → Telegram Forwarder Bot
============================================
Receives payment slip images from Facebook Page Messenger via webhook,
detects whether the slip is Thai Baht (ဘတ်) or Myanmar Kyat (ကျပ်)
using image color analysis (no external API needed),
and forwards the image with sender info to the appropriate Telegram group.
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
# Currency detection keywords (from caption text)
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
    "wave money", "wavepay", "ok dollar", "onepay",
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
    """
    Detect currency by analyzing dominant colors in the image.
    KBZ Bank (Myanmar Kyat) slips are predominantly blue (#1a3a6b or similar deep blue).
    Thai bank slips: SCB=purple, KBank=green, BBL=blue (lighter), Promptpay=various.
    
    Strategy:
    - Count blue pixels in the top portion of the image (where bank logo usually is)
    - KBZ blue is a specific deep/navy blue: R<100, G<120, B>150
    - Thai bank logos tend to be lighter or different hues
    """
    if not PIL_AVAILABLE:
        logger.warning("PIL not available - cannot analyze image colors")
        return "unknown"

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = img.size

        # Analyze top 30% of image (where bank logo/header usually is)
        top_height = int(height * 0.30)
        top_region = img.crop((0, 0, width, top_height))
        pixels = list(top_region.getdata())

        total = len(pixels)
        if total == 0:
            return "unknown"

        # Count color categories
        kbz_blue = 0      # KBZ deep blue: R<120, G<150, B>150
        thai_purple = 0   # SCB purple: R>100, G<100, B>100
        thai_green = 0    # KBank green: G > R+50 and G > B+30
        white_count = 0   # White/light background

        for r, g, b in pixels:
            brightness = (r + g + b) / 3

            # Skip very dark or very white pixels
            if brightness > 230:
                white_count += 1
                continue
            if brightness < 20:
                continue

            # KBZ-style deep blue (navy blue)
            if b > 120 and r < 120 and g < 140 and b > r + 30 and b > g + 10:
                kbz_blue += 1
            # SCB purple
            elif r > 100 and b > 100 and g < 80:
                thai_purple += 1
            # KBank green
            elif g > r + 40 and g > b + 20 and g > 100:
                thai_green += 1

        non_white = total - white_count
        if non_white == 0:
            return "unknown"

        kbz_ratio = kbz_blue / non_white
        thai_ratio = (thai_purple + thai_green) / non_white

        logger.info(f"Color analysis - KBZ blue: {kbz_ratio:.3f}, Thai: {thai_ratio:.3f}, total pixels: {total}")

        # Decision
        if kbz_ratio > 0.08:
            logger.info("Detected: Myanmar Kyat (KBZ blue dominant)")
            return "kyat"
        elif thai_ratio > 0.05:
            logger.info("Detected: Thai Baht (Thai bank color)")
            return "baht"
        elif kbz_ratio > thai_ratio and kbz_ratio > 0.03:
            logger.info("Detected: Myanmar Kyat (KBZ blue slightly dominant)")
            return "kyat"
        else:
            logger.info("Color analysis inconclusive")
            return "unknown"

    except Exception as e:
        logger.error(f"Image color analysis error: {e}")
        return "unknown"


def detect_currency(image_bytes: bytes, caption_text: str) -> str:
    """
    Try text detection first, then image color analysis.
    """
    # 1. Try caption text first (most reliable if user adds it)
    currency = detect_currency_from_text(caption_text)
    if currency != "unknown":
        logger.info(f"Currency from text: {currency}")
        return currency

    # 2. Try image color analysis
    if image_bytes:
        currency = detect_currency_from_image(image_bytes)
        if currency != "unknown":
            logger.info(f"Currency from image: {currency}")
            return currency

    return "unknown"


def get_fb_user_profile(sender_id: str) -> dict:
    profile = {"name": f"FB User {sender_id}", "id": sender_id}
    if not FB_PAGE_ACCESS_TOKEN:
        return profile
    try:
        url = f"{FB_GRAPH_API}/{sender_id}"
        params = {"fields": "name", "access_token": FB_PAGE_ACCESS_TOKEN}
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            profile["name"] = data.get("name", profile["name"])
    except Exception as e:
        logger.error(f"FB profile error: {e}")
    return profile


def download_fb_image(image_url: str) -> bytes:
    try:
        resp = requests.get(image_url, timeout=30)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        logger.error(f"Image download error: {e}")
    return None


def send_telegram_photo(chat_id: str, photo_bytes: bytes, caption: str) -> bool:
    try:
        url = f"{TG_API_BASE}/sendPhoto"
        files = {"photo": ("slip.jpg", photo_bytes, "image/jpeg")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        resp = requests.post(url, files=files, data=data, timeout=30)
        result = resp.json()
        if result.get("ok"):
            logger.info(f"Photo sent to {chat_id}")
            return True
        else:
            logger.error(f"Telegram error: {result}")
            return False
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return False


def send_fb_reply(sender_id: str, text: str):
    if not FB_PAGE_ACCESS_TOKEN:
        return
    try:
        url = f"{FB_GRAPH_API}/me/messages"
        params = {"access_token": FB_PAGE_ACCESS_TOKEN}
        payload = {"recipient": {"id": sender_id}, "message": {"text": text}}
        requests.post(url, params=params, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"FB reply error: {e}")


def forward_image_to_telegram(image_url: str, sender_profile: dict, caption_text: str = ""):
    image_bytes = download_fb_image(image_url)
    if not image_bytes:
        logger.error("Image download failed")
        return False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    currency = detect_currency(image_bytes, caption_text)

    if currency == "baht":
        currency_label = "ထိုင်းဘတ် (฿)"
    elif currency == "kyat":
        currency_label = "မြန်မာကျပ် (K)"
    else:
        currency_label = "မသိ (Unknown)"

    tg_caption = (
        f"📋 <b>ငွေပေးချေမှု ပြေစာ</b>\n\n"
        f"👤 ပို့သူ: {sender_profile.get('name', 'Unknown')}\n"
        f"🆔 FB ID: {sender_profile.get('id', 'Unknown')}\n"
        f"💰 ငွေကြေး: {currency_label}\n"
        f"🕐 အချိန်: {now_str}"
    )

    if caption_text:
        tg_caption += f"\n📝 မက်ဆေ့ချ်: {caption_text}"

    success = False

    if currency == "kyat":
        if TG_KYAT_GROUP:
            success = send_telegram_photo(TG_KYAT_GROUP, image_bytes, tg_caption)
            if success:
                logger.info("Forwarded to Kyat group")
    elif currency == "baht":
        if TG_BAHT_GROUP:
            success = send_telegram_photo(TG_BAHT_GROUP, image_bytes, tg_caption)
            if success:
                logger.info("Forwarded to Baht group")
    else:
        # Unknown - send to both groups
        s1 = send_telegram_photo(TG_BAHT_GROUP, image_bytes, tg_caption) if TG_BAHT_GROUP else False
        s2 = send_telegram_photo(TG_KYAT_GROUP, image_bytes, tg_caption) if TG_KYAT_GROUP else False
        success = s1 or s2
        if success:
            logger.info("Forwarded to both groups (unknown currency)")

    return success


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
        if not body or body.get("object") != "page":
            return "OK", 200

        for entry in body.get("entry", []):
            for event in entry.get("messaging", []):
                process_messaging_event(event)

        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "OK", 200


def process_messaging_event(event: dict):
    try:
        sender_id = event.get("sender", {}).get("id", "")
        if sender_id == FB_PAGE_ID:
            return

        message = event.get("message", {})
        if not message:
            return

        sender_profile = get_fb_user_profile(sender_id)
        logger.info(f"Message from: {sender_profile.get('name', sender_id)}")

        attachments = message.get("attachments", [])
        message_text = message.get("text", "")

        for attachment in attachments:
            if attachment.get("type") == "image":
                image_url = attachment.get("payload", {}).get("url", "")
                if image_url:
                    success = forward_image_to_telegram(
                        image_url=image_url,
                        sender_profile=sender_profile,
                        caption_text=message_text,
                    )
                    if success and FB_PAGE_ACCESS_TOKEN:
                        send_fb_reply(sender_id, "✅ စလစ်ဓာတ်ပုံ လက်ခံရရှိပါပြီ။")

    except Exception as e:
        logger.error(f"Event processing error: {e}")


# =============================================================================
# Main
# =============================================================================

def main():
    logger.info("Facebook to Telegram Bot started")
    logger.info(f"PIL available: {PIL_AVAILABLE}")
    logger.info(f"Baht group: {TG_BAHT_GROUP}")
    logger.info(f"Kyat group: {TG_KYAT_GROUP or '(not set)'}")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
