#!/usr/bin/env python3
"""
Thai Baht Payment Slip Tracking Telegram Bot
Monitors group messages, tracks payment slips, detects duplicates, and provides reporting.
All responses in Myanmar language (Burmese).
Text-based slip detection only (no OCR/image processing).
"""

import os
import sqlite3
import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Myanmar language strings
MYANMAR_STRINGS = {
    'welcome': 'ကြိုဆိုပါသည်။ ငွေလွှဲမှတ်တမ်းစနစ်သို့',
    'help_title': '📋 အကူအညီ - အမိန့်များ',
    'help_text': '''
/summary - ယနေ့၏အကျဉ်းချုပ်ပြသ (စုစုပေါင်းအဝင်၊ စုစုပေါင်းအထုတ်၊ လက်ကျန်)
/list [YYYY-MM-DD] - သတ်မှတ်ထားသောနေ့ရက်အတွက်ငွေလွှဲများစာရင်း (ပုံသဏ္ဍာန်: YYYY-MM-DD)
/check [slip_id] - အတည်းအလျှင်း ID ရှိမရှိ စစ်ဆေးရန်
/balance - လက်ကျန်ငွေပြသ
/help - အကူအညီပြသ
''',
    'summary_header': '📊 ယနေ့၏အကျဉ်းချုပ်',
    'summary_in': '✅ စုစုပေါင်းအဝင်: {amount} ဘတ်',
    'summary_out': '❌ စုစုပေါင်းအထုတ်: {amount} ဘတ်',
    'summary_balance': '💰 လက်ကျန်: {amount} ဘတ်',
    'list_header': '📝 {date} အတွက်ငွေလွှဲများ',
    'list_empty': 'ဤနေ့ရက်တွင်ငွေလွှဲမှတ်တမ်းမရှိပါ။',
    'list_item': '{time} | {type} | {amount} ဘတ် | ID: {slip_id} | အသုံးပြုသူ: {username}',
    'check_found': '✅ အတည်းအလျှင်း ID {slip_id} ရှိပါသည်။\nအချက်အလက်: {details}',
    'check_not_found': '❌ အတည်းအလျှင်း ID {slip_id} မတွေ့ရှိပါ။',
    'balance_current': '💰 လက်ကျန်ငွေ: {amount} ဘတ်',
    'duplicate_alert': '⚠️ အသိ警告: အတည်းအလျှင်း ID {slip_id} အလယ်တွင်ရှိပြီးဖြစ်သည်။\nအရင်းအမြစ်: {previous_user} ({previous_time})',
    'invalid_slip_alert': '⚠️ အသိ: အတည်းအလျှင်းအချက်အလက်သည်မှားမှားအားအားဖြစ်နိုင်သည်။ ကျေးဇူးပြု၍ အတည်းအလျှင်းကိုအတည်ပြုပါ။',
    'slip_recorded': '✅ အတည်းအလျှင်း မှတ်တမ်းတင်ပြီးပါပြီ။\nID: {slip_id}\nအခြေအနေ: {type}\nအရေအတွက်: {amount} ဘတ်',
    'error_invalid_date': '❌ အမှားအယွင်း: နေ့ရက်ပုံစံမှားသည်။ YYYY-MM-DD ကိုအသုံးပြုပါ။',
    'error_invalid_slip_id': '❌ အမှားအယွင်း: အတည်းအလျှင်း ID မရှိပါ။',
    'error_processing': '❌ အမှားအယွင်း: အချက်အလက်ကိုလုပ်ဆောင်နိုင်ခြင်းမရှိ။ ကျေးဇူးပြု၍ နောက်ပိုင်းတွင်ထပ်မံကြိုးစားပါ။',
    'invalid_amount': '❌ အမှားအယွင်း: ငွေပမာဏမှားသည်။ ကျေးဇူးပြု၍ အရေအတွက်ကိုအတည်ပြုပါ။',
    'incoming': 'အဝင်',
    'outgoing': 'အထုတ်',
}

class SlipDatabase:
    """Database handler for payment slip records"""
    
    def __init__(self, db_path: str = '/home/ubuntu/slip_records.db'):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """Initialize database schema"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create transactions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slip_id TEXT UNIQUE NOT NULL,
                timestamp DATETIME NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                amount REAL NOT NULL,
                transaction_type TEXT NOT NULL,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create duplicate alerts table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS duplicate_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slip_id TEXT NOT NULL,
                first_user_id INTEGER,
                first_username TEXT,
                first_timestamp DATETIME,
                duplicate_user_id INTEGER,
                duplicate_username TEXT,
                duplicate_timestamp DATETIME,
                alert_time DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create suspicious slips table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS suspicious_slips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slip_id TEXT,
                user_id INTEGER,
                username TEXT,
                reason TEXT,
                timestamp DATETIME,
                alert_time DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")
    
    def add_transaction(self, slip_id: str, user_id: int, username: str, 
                       amount: float, transaction_type: str, notes: str = '') -> Tuple[bool, str]:
        """Add a new transaction record"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Check for duplicate slip ID
            cursor.execute('SELECT * FROM transactions WHERE slip_id = ?', (slip_id,))
            existing = cursor.fetchone()
            
            if existing:
                # Record duplicate alert
                cursor.execute('''
                    INSERT INTO duplicate_alerts 
                    (slip_id, first_user_id, first_username, first_timestamp, 
                     duplicate_user_id, duplicate_username, duplicate_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (slip_id, existing[2], existing[3], existing[1], 
                      user_id, username, datetime.now()))
                conn.commit()
                conn.close()
                return False, f"duplicate:{slip_id}:{existing[3]}:{existing[1]}"
            
            # Insert new transaction
            cursor.execute('''
                INSERT INTO transactions 
                (slip_id, timestamp, user_id, username, amount, transaction_type, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (slip_id, datetime.now(), user_id, username, amount, transaction_type, notes))
            
            conn.commit()
            conn.close()
            return True, slip_id
        except sqlite3.IntegrityError as e:
            logger.error(f"Database integrity error: {e}")
            return False, f"error:{str(e)}"
        except Exception as e:
            logger.error(f"Error adding transaction: {e}")
            return False, f"error:{str(e)}"
    
    def get_today_summary(self) -> Dict[str, float]:
        """Get today's transaction summary"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            today = datetime.now().date()
            
            # Get incoming total
            cursor.execute('''
                SELECT SUM(amount) FROM transactions 
                WHERE DATE(timestamp) = ? AND transaction_type = ?
            ''', (today, 'အဝင်'))
            incoming = cursor.fetchone()[0] or 0
            
            # Get outgoing total
            cursor.execute('''
                SELECT SUM(amount) FROM transactions 
                WHERE DATE(timestamp) = ? AND transaction_type = ?
            ''', (today, 'အထုတ်'))
            outgoing = cursor.fetchone()[0] or 0
            
            conn.close()
            return {
                'incoming': incoming,
                'outgoing': outgoing,
                'balance': incoming - outgoing
            }
        except Exception as e:
            logger.error(f"Error getting summary: {e}")
            return {'incoming': 0, 'outgoing': 0, 'balance': 0}
    
    def get_transactions_by_date(self, date_str: str) -> List[Dict]:
        """Get all transactions for a specific date"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT slip_id, timestamp, username, amount, transaction_type 
                FROM transactions 
                WHERE DATE(timestamp) = ?
                ORDER BY timestamp DESC
            ''', (date_str,))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    'slip_id': row[0],
                    'timestamp': row[1],
                    'username': row[2],
                    'amount': row[3],
                    'type': row[4]
                })
            
            conn.close()
            return results
        except Exception as e:
            logger.error(f"Error getting transactions: {e}")
            return []
    
    def check_slip_id(self, slip_id: str) -> Optional[Dict]:
        """Check if a slip ID exists"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT slip_id, timestamp, username, amount, transaction_type 
                FROM transactions 
                WHERE slip_id = ?
            ''', (slip_id,))
            
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return {
                    'slip_id': result[0],
                    'timestamp': result[1],
                    'username': result[2],
                    'amount': result[3],
                    'type': result[4]
                }
            return None
        except Exception as e:
            logger.error(f"Error checking slip ID: {e}")
            return None
    
    def get_total_balance(self) -> float:
        """Get total balance across all time"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Get total incoming
            cursor.execute('''
                SELECT SUM(amount) FROM transactions WHERE transaction_type = ?
            ''', ('အဝင်',))
            incoming = cursor.fetchone()[0] or 0
            
            # Get total outgoing
            cursor.execute('''
                SELECT SUM(amount) FROM transactions WHERE transaction_type = ?
            ''', ('အထုတ်',))
            outgoing = cursor.fetchone()[0] or 0
            
            conn.close()
            return incoming - outgoing
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return 0


class SlipDetector:
    """Detect and extract payment slip information from text"""
    
    # Thai Baht slip ID patterns
    SLIP_ID_PATTERNS = [
        r'(?:slip|id|no\.?|#)\s*:?\s*([A-Z0-9]{6,20})',  # Explicit slip ID
        r'([A-Z0-9]{8,16})',  # Generic alphanumeric ID
    ]
    
    # Amount patterns (Thai Baht)
    AMOUNT_PATTERNS = [
        r'(?:amount|total|sum|baht|บาท)\s*:?\s*([0-9]+(?:[.,][0-9]{2})?)',
        r'([0-9]+(?:[.,][0-9]{2})?)\s*(?:baht|บาท|฿)',
        r'(?:฿|baht)\s*([0-9]+(?:[.,][0-9]{2})?)',
    ]
    
    @staticmethod
    def extract_from_text(text: str) -> Tuple[Optional[str], Optional[float]]:
        """Extract slip ID and amount from text"""
        slip_id = None
        amount = None
        
        text_upper = text.upper()
        
        # Try to extract slip ID
        for pattern in SlipDetector.SLIP_ID_PATTERNS:
            match = re.search(pattern, text_upper, re.IGNORECASE)
            if match:
                slip_id = match.group(1).strip()
                if len(slip_id) >= 6:  # Minimum length for valid slip ID
                    break
        
        # Try to extract amount
        for pattern in SlipDetector.AMOUNT_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                amount_str = match.group(1).replace(',', '.')
                try:
                    amount = float(amount_str)
                    if 0 < amount < 1000000:  # Reasonable amount range
                        break
                except ValueError:
                    continue
        
        return slip_id, amount
    
    @staticmethod
    def is_valid_slip(slip_id: Optional[str], amount: Optional[float]) -> bool:
        """Validate slip information"""
        if not slip_id or not amount:
            return False
        
        # Validate slip ID format
        if not (6 <= len(slip_id) <= 20):
            return False
        
        # Validate amount
        if not (0 < amount < 1000000):
            return False
        
        return True


class TelegramSlipBot:
    """Main Telegram bot handler"""
    
    def __init__(self, token: str, special_user: str):
        self.token = token
        self.special_user = special_user
        self.db = SlipDatabase()
        self.detector = SlipDetector()
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        await update.message.reply_text(
            f"{MYANMAR_STRINGS['welcome']}\n\n{MYANMAR_STRINGS['help_text']}"
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = f"{MYANMAR_STRINGS['help_title']}\n{MYANMAR_STRINGS['help_text']}"
        await update.message.reply_text(help_text)
    
    async def summary_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /summary command"""
        try:
            summary = self.db.get_today_summary()
            
            response = f"{MYANMAR_STRINGS['summary_header']}\n\n"
            response += f"{MYANMAR_STRINGS['summary_in'].format(amount=summary['incoming'])}\n"
            response += f"{MYANMAR_STRINGS['summary_out'].format(amount=summary['outgoing'])}\n"
            response += f"{MYANMAR_STRINGS['summary_balance'].format(amount=summary['balance'])}"
            
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error in summary command: {e}")
            await update.message.reply_text(MYANMAR_STRINGS['error_processing'])
    
    async def list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command"""
        try:
            # Get date from arguments or use today
            if context.args:
                date_str = context.args[0]
                # Validate date format
                try:
                    datetime.strptime(date_str, '%Y-%m-%d')
                except ValueError:
                    await update.message.reply_text(MYANMAR_STRINGS['error_invalid_date'])
                    return
            else:
                date_str = datetime.now().strftime('%Y-%m-%d')
            
            transactions = self.db.get_transactions_by_date(date_str)
            
            if not transactions:
                response = f"{MYANMAR_STRINGS['list_header'].format(date=date_str)}\n\n"
                response += MYANMAR_STRINGS['list_empty']
            else:
                response = f"{MYANMAR_STRINGS['list_header'].format(date=date_str)}\n\n"
                for tx in transactions:
                    time_str = tx['timestamp'].split(' ')[1] if ' ' in tx['timestamp'] else tx['timestamp']
                    item_str = MYANMAR_STRINGS['list_item'].format(
                        time=time_str,
                        type=tx['type'],
                        amount=tx['amount'],
                        slip_id=tx['slip_id'],
                        username=tx['username'] or 'Unknown'
                    )
                    response += f"{item_str}\n"
            
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error in list command: {e}")
            await update.message.reply_text(MYANMAR_STRINGS['error_processing'])
    
    async def check_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /check command"""
        try:
            if not context.args:
                await update.message.reply_text(MYANMAR_STRINGS['error_invalid_slip_id'])
                return
            
            slip_id = context.args[0]
            result = self.db.check_slip_id(slip_id)
            
            if result:
                details = f"{result['type']} | {result['amount']} ဘတ် | {result['username']} | {result['timestamp']}"
                response = MYANMAR_STRINGS['check_found'].format(slip_id=slip_id, details=details)
            else:
                response = MYANMAR_STRINGS['check_not_found'].format(slip_id=slip_id)
            
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error in check command: {e}")
            await update.message.reply_text(MYANMAR_STRINGS['error_processing'])
    
    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /balance command"""
        try:
            balance = self.db.get_total_balance()
            response = MYANMAR_STRINGS['balance_current'].format(amount=balance)
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error in balance command: {e}")
            await update.message.reply_text(MYANMAR_STRINGS['error_processing'])
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming text messages with payment slip information"""
        try:
            if not update.message or not update.message.text:
                return
            
            # Determine transaction type based on sender
            sender_username = update.message.from_user.username or f"user_{update.message.from_user.id}"
            is_special_user = sender_username == self.special_user or \
                            (self.special_user.startswith('@') and sender_username == self.special_user[1:])
            
            transaction_type = MYANMAR_STRINGS['outgoing'] if is_special_user else MYANMAR_STRINGS['incoming']
            
            # Extract information from text message
            slip_id, amount = self.detector.extract_from_text(update.message.text)
            
            # Validate and store if valid slip information found
            if self.detector.is_valid_slip(slip_id, amount):
                success, result = self.db.add_transaction(
                    slip_id=slip_id,
                    user_id=update.message.from_user.id,
                    username=sender_username,
                    amount=amount,
                    transaction_type=transaction_type
                )
                
                if success:
                    response = MYANMAR_STRINGS['slip_recorded'].format(
                        slip_id=slip_id,
                        type=transaction_type,
                        amount=amount
                    )
                    await update.message.reply_text(response)
                    logger.info(f"Recorded slip {slip_id} from {sender_username}")
                elif result.startswith('duplicate:'):
                    parts = result.split(':')
                    dup_slip_id = parts[1]
                    prev_user = parts[2]
                    alert = MYANMAR_STRINGS['duplicate_alert'].format(
                        slip_id=dup_slip_id,
                        previous_user=prev_user,
                        previous_time=parts[3] if len(parts) > 3 else 'Unknown'
                    )
                    await update.message.reply_text(alert)
                    logger.warning(f"Duplicate slip detected: {dup_slip_id}")
            else:
                # Only alert if message looks like it might be a slip
                if slip_id or amount:
                    await update.message.reply_text(MYANMAR_STRINGS['invalid_slip_alert'])
                    logger.warning(f"Invalid slip from {sender_username}: ID={slip_id}, Amount={amount}")
        
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    def run(self):
        """Run the bot"""
        application = Application.builder().token(self.token).build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("summary", self.summary_command))
        application.add_handler(CommandHandler("list", self.list_command))
        application.add_handler(CommandHandler("check", self.check_command))
        application.add_handler(CommandHandler("balance", self.balance_command))
        
        # Add message handler for text messages only
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            self.handle_message
        ))
        
        logger.info("Bot started and polling...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    """Main entry point"""
    # Configuration
    BOT_TOKEN = "8214915771:AAEuffebveqtWAQpFmeHE_SxjeqD7Foyxyw"
    SPECIAL_USER = "Stttt298"  # Without @ symbol
    
    # Initialize and run bot
    bot = TelegramSlipBot(
        token=BOT_TOKEN,
        special_user=SPECIAL_USER
    )
    
    bot.run()


if __name__ == "__main__":
    main()
