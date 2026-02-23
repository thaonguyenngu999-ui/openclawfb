"""
TelegramMixin - Telegram messaging helpers (edit message, send photo).
"""

import os
from typing import Dict, Any


class TelegramMixin:
    """Telegram bot integration helpers."""

    BOT_TOKEN = "6891995669:AAHOlqrRiSoNzI1FcyqWemvpo5ZRxV4AKDI"

    @staticmethod
    def _telegram_edit_message(chat_id, message_id, text,
                                bot_token="6891995669:AAHOlqrRiSoNzI1FcyqWemvpo5ZRxV4AKDI"):
        """Edit a Telegram message (fire-and-forget for progress updates)."""
        import requests as req
        try:
            url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
            req.post(url, json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown"
            }, timeout=10)
        except Exception as e:
            print(f"[API] Telegram edit error: {e}")

    @staticmethod
    def send_telegram_photo(filepath: str, chat_id: str, caption: str = "",
                            bot_token: str = "6891995669:AAHOlqrRiSoNzI1FcyqWemvpo5ZRxV4AKDI") -> Dict[str, Any]:
        """
        Gửi ảnh screenshot lên Telegram.
        filepath: đường dẫn file PNG trên disk
        chat_id: Telegram chat ID
        caption: chú thích cho ảnh (optional)
        """
        import requests as req

        try:
            if not os.path.exists(filepath):
                return {"success": False, "error": f"File not found: {filepath}"}

            url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

            with open(filepath, 'rb') as photo:
                files = {'photo': (os.path.basename(filepath), photo, 'image/png')}
                data = {'chat_id': chat_id}
                if caption:
                    data['caption'] = caption[:1024]  # Telegram caption limit
                    data['parse_mode'] = 'HTML'

                resp = req.post(url, data=data, files=files, timeout=30)
                result = resp.json()

            if result.get('ok'):
                return {"success": True, "message_id": result.get('result', {}).get('message_id')}
            else:
                return {"success": False, "error": result.get('description', 'Unknown Telegram error')}

        except Exception as e:
            return {"success": False, "error": str(e)}
