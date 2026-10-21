import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class TelegramDeliveryError(Exception):
    """Telegram did not confirm successful message delivery."""


def send_telegram_message(text: str) -> int:
    """Send plain text to the library chat and return its Telegram message ID."""
    token = settings.TELEGRAM_BOT_TOKEN.strip()
    chat_id = settings.TELEGRAM_CHAT_ID.strip()
    if not token or not chat_id:
        raise ImproperlyConfigured(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before sending notifications."
        )
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Message must not be empty.")

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        raise TelegramDeliveryError("Telegram request failed.") from None

    try:
        payload = response.json()
    except ValueError:
        raise TelegramDeliveryError("Telegram returned invalid JSON.") from None

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramDeliveryError("Telegram did not accept the message.")

    result = payload.get("result")
    if not isinstance(result, dict) or type(result.get("message_id")) is not int:
        raise TelegramDeliveryError("Telegram returned no valid message ID.")

    return result["message_id"]
