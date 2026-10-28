
import json
import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# Load configuration from .env in this directory
APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

BASE_URL = "https://app.all-hands.dev"
API_URL = f"{BASE_URL}/api/v1/app-conversations/search"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OPENHANDS_API_KEY = os.getenv("OPENHANDS_API_KEY", "").strip()

POLL_SECONDS = 30
PAGE_SIZE = 50
STATE_FILE = APP_DIR / "openhands_notifier_state.json"

TERMINAL_STATUSES = {
    "finished",
    "error",
    "stuck",
    "waiting_for_confirmation",
}

MESSAGES = {
    "finished": "✅ انتهى تنفيذ المهمة في OpenHands.",
    "error": "❌ حدث خطأ أثناء تنفيذ المهمة.",
    "stuck": "⚠️ يبدو أن وكيل OpenHands عالق.",
    "waiting_for_confirmation": "⏸️ الوكيل ينتظر تأكيدك.",
    "sandbox_error": "❌ حدث خطأ في بيئة تنفيذ OpenHands.",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)


def validate_config():
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not OPENHANDS_API_KEY:
        missing.append("OPENHANDS_API_KEY")

    if missing:
        raise RuntimeError(
            "Missing .env settings: " + ", ".join(missing)
        )


session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {OPENHANDS_API_KEY}",
    "Accept": "application/json",
})


def send_telegram(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected the message")


def load_state():
    if not STATE_FILE.exists():
        return None

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Cannot read state file; refusing to reset notification history."
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("Invalid state file")

    return data


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_file.replace(STATE_FILE)


def fetch_all_conversations():
    conversations = []
    page_id = None
    seen_cursors = set()

    while True:
        params = {"limit": PAGE_SIZE}

        if page_id:
            if page_id in seen_cursors:
                raise RuntimeError("Repeated pagination cursor")
            seen_cursors.add(page_id)
            params["page_id"] = page_id

        response = session.get(
            API_URL,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError("Unexpected API response")

        items = data.get("items")
        if not isinstance(items, list):
            raise RuntimeError("Missing conversation items")

        conversations.extend(items)

        page_id = data.get("next_page_id")
        if not page_id:
            break

    return conversations


def get_status(conversation):
    status = str(
        conversation.get("execution_status") or "unknown"
    ).lower()

    sandbox = str(
        conversation.get("sandbox_status") or ""
    ).upper()

    if sandbox in {"ERROR", "MISSING"}:
        return "sandbox_error"

    return status


def notify(conversation, status):
    conversation_id = conversation.get("id")
    title = conversation.get("title") or "محادثة بدون عنوان"

    message = (
        f"{MESSAGES.get(status, '⚠️ تغيرت حالة المحادثة.')}\n\n"
        f"المحادثة: {title}\n"
        f"الحالة: {status}\n"
        f"{BASE_URL}/conversations/{conversation_id}"
    )

    send_telegram(message)


def main():
    validate_config()
    state = load_state()

    # Verify Telegram before starting the monitoring loop.
    send_telegram(
        "🤖 بدأ مراقب OpenHands.\n"
        "جارٍ التحقق من إمكانية قراءة المحادثات."
    )

    while True:
        try:
            conversations = fetch_all_conversations()

            # First run: record existing statuses without
            # notifying for every old completed conversation.
            if state is None:
                state = {}

                for conversation in conversations:
                    conversation_id = conversation.get("id")
                    if conversation_id:
                        state[conversation_id] = get_status(
                            conversation
                        )

                save_state(state)

                logging.info(
                    "Initial baseline saved: %d conversations",
                    len(state),
                )

            else:
                updated = dict(state)

                for conversation in conversations:
                    conversation_id = conversation.get("id")
                    if not conversation_id:
                        continue

                    new_status = get_status(conversation)
                    old_status = state.get(conversation_id)

                    is_new = old_status is None
                    became_terminal = (
                        new_status in TERMINAL_STATUSES
                        and old_status not in TERMINAL_STATUSES
                    )

                    # Notify for new completed conversations and
                    # conversations transitioning to a terminal state.
                    should_notify = (
                        is_new and new_status in TERMINAL_STATUSES
                    ) or became_terminal

                    if should_notify:
                        notify(conversation, new_status)

                    updated[conversation_id] = new_status

                save_state(updated)
                state = updated

            logging.info(
                "Scan complete: %d conversations",
                len(conversations),
            )

        except KeyboardInterrupt:
            logging.info("Notifier stopped.")
            break

        except Exception:
            logging.exception("Monitoring failed; will retry.")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
