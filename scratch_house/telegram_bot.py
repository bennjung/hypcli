from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

LOG = logging.getLogger("scratch_house.telegram_bot")


class LinkApiClient:
    def __init__(self, base_url: str, bearer_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token.strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    async def list_sessions(self) -> tuple[bool, list[dict[str, Any]] | str]:
        url = f"{self.base_url}/api/link/sessions"
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.get(url, timeout=8) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200:
                        return False, str(data.get("error", f"status {response.status}"))
                    sessions = data.get("sessions", [])
                    if not isinstance(sessions, list):
                        return False, "invalid sessions payload"
                    return True, sessions
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def assign_session(
        self,
        session_id: str,
        telegram_user_id: int,
        telegram_display_name: str,
    ) -> tuple[bool, str]:
        url = f"{self.base_url}/api/link/assign"
        payload = {
            "session_id": session_id,
            "telegram_user_id": str(telegram_user_id),
            "telegram_display_name": telegram_display_name,
        }
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.post(
                    url,
                    data=json.dumps(payload),
                    timeout=8,
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200:
                        return False, str(data.get("error", f"status {response.status}"))
                    linked_name = data.get("name", telegram_display_name)
                    return True, f"Linked successfully as {linked_name}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


def format_display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "unknown"
    if user.full_name:
        return user.full_name
    if user.username:
        return user.username
    return f"tg-{user.id}"


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Commands\n"
        "/link - Show pending CLI sessions and link one\n"
        "/help - Show this help"
    )


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api_client: LinkApiClient = context.bot_data["api_client"]

    ok, data = await api_client.list_sessions()
    if not ok:
        await update.effective_message.reply_text(f"Failed to load sessions: {data}")
        return

    sessions = data
    if not sessions:
        await update.effective_message.reply_text(
            "No pending sessions.\n"
            "1) Run scratch-house-client --link-telegram\n"
            "2) Run /link again"
        )
        return

    rows = []
    for item in sessions[:20]:
        sid = str(item.get("session_id", ""))
        device = str(item.get("device_name", "unknown-device"))
        age = int(item.get("age_seconds", 0))
        label = f"{device} [{sid}] ({age}s)"
        rows.append([InlineKeyboardButton(label, callback_data=f"link:{sid}")])

    await update.effective_message.reply_text(
        "Choose a session to link:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = str(query.data or "")
    if not data.startswith("link:"):
        await query.edit_message_text("Invalid callback")
        return

    session_id = data.split(":", 1)[1].strip().upper()
    if not session_id:
        await query.edit_message_text("Invalid session id")
        return

    user = update.effective_user
    if not user:
        await query.edit_message_text("Missing Telegram user")
        return

    api_client: LinkApiClient = context.bot_data["api_client"]
    ok, message = await api_client.assign_session(
        session_id=session_id,
        telegram_user_id=int(user.id),
        telegram_display_name=format_display_name(update),
    )
    if not ok:
        await query.edit_message_text(f"Link failed: {message}")
        return

    await query.edit_message_text(f"{message}\nSession: {session_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scratch House Telegram link bot")
    parser.add_argument(
        "--bot-token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram bot token (or TELEGRAM_BOT_TOKEN)",
    )
    parser.add_argument(
        "--link-api-base",
        default=os.environ.get("LINK_API_BASE", "http://127.0.0.1:8787"),
        help="Scratch House link API base URL",
    )
    parser.add_argument(
        "--link-api-token",
        default=os.environ.get("LINK_API_TOKEN", ""),
        help="Bearer token for link API",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    if not args.bot_token:
        parser.error("--bot-token is required (or set TELEGRAM_BOT_TOKEN)")

    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    application = Application.builder().token(args.bot_token).build()
    application.bot_data["api_client"] = LinkApiClient(
        base_url=str(args.link_api_base),
        bearer_token=str(args.link_api_token),
    )

    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", cmd_help))
    application.add_handler(CommandHandler("link", cmd_link))
    application.add_handler(CallbackQueryHandler(on_link_callback, pattern=r"^link:"))

    LOG.info("Telegram bot started")
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
