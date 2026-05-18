import os
import logging
import requests
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Map slot strings ('A', 'B', 'C' or '1', '2', '3') to respective chat IDs
CHATS = {
    'default': os.getenv('TELEGRAM_CHAT_ID'),
    '2': os.getenv('TELEGRAM_CHAT_ID_2'),
    '3': os.getenv('TELEGRAM_CHAT_ID_3')
}

logger = logging.getLogger(__name__)

def send_telegram_message(message: str, target_chat: str = 'default'):
    """Sends a message to the configured Telegram chat."""
    chat_id = CHATS.get(target_chat) or CHATS.get('default')
    
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        logger.warning(f"Telegram not configured for {target_chat}. Would have sent: {message}")
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False
