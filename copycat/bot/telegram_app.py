"""python-telegram-bot v21 front-end. Thin: everything goes through Commands."""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from ..app import build
from ..config import AppConfig
from ..services.runner import Runner
from .commands import Commands, Reply

log = logging.getLogger(__name__)


def markup(buttons: list[list[tuple[str, str]]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = []
    for row in buttons:
        r = []
        for label, data in row:
            if data.startswith("url:"):
                url = data[4:]
                if url.startswith("http"):
                    r.append(InlineKeyboardButton(label, url=url))
            else:
                r.append(InlineKeyboardButton(label, callback_data=data[:64]))
        if r:
            rows.append(r)
    return InlineKeyboardMarkup(rows) if rows else None


class TelegramNotifier:
    def __init__(self) -> None:
        self.app: Application | None = None
        self.repo: Any = None

    def _chat(self, uid: int) -> int:
        for u in self.repo.list_users():
            if u["user_id"] == uid:
                return u["chat_id"]
        return uid

    async def send(self, user_id: int, text: str, buttons: list | None = None) -> None:
        assert self.app
        for attempt in range(3):
            try:
                await self.app.bot.send_message(self._chat(user_id), text[:4096], parse_mode=ParseMode.HTML,
                                                reply_markup=markup(buttons), disable_web_page_preview=True)
                return
            except RetryAfter as e:
                import asyncio
                await asyncio.sleep(float(e.retry_after) + 0.5)
            except Forbidden:
                log.info("user %s blocked the bot", user_id)
                return
            except BadRequest as e:
                log.warning("bad request sending to %s: %s", user_id, e)
                await self.app.bot.send_message(self._chat(user_id), text[:4000])
                return

    async def send_photo(self, user_id: int, path: str, caption: str = "") -> None:
        assert self.app
        with open(path, "rb") as f:
            await self.app.bot.send_photo(self._chat(user_id), f, caption=caption[:1024], parse_mode=ParseMode.HTML)

    async def send_document(self, user_id: int, path: str, caption: str = "") -> None:
        assert self.app
        with open(path, "rb") as f:
            await self.app.bot.send_document(self._chat(user_id), f, caption=caption[:1024], parse_mode=ParseMode.HTML)


async def _deliver(notifier: TelegramNotifier, uid: int, replies: list[Reply]) -> None:
    for r in replies:
        if r.photo:
            await notifier.send_photo(uid, r.photo, r.text)
        elif r.document:
            await notifier.send_document(uid, r.document, r.text)
        elif r.text:
            await notifier.send(uid, r.text, r.buttons)


def run(cfg: AppConfig) -> None:
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required (see README → BotFather).")
    notifier = TelegramNotifier()
    engine, http = build(cfg, notifier)
    notifier.repo = engine.repo
    runner = Runner(engine)
    commands = Commands(engine, runner)

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not update.message:
            return
        uid = update.effective_user.id
        try:
            replies = await commands.handle_text(uid, update.effective_chat.id, update.message.text or "")
        except Exception:  # noqa: BLE001
            log.exception("command failed")
            replies = [Reply("⚠️ Something went wrong handling that. Try again.")]
        await _deliver(notifier, uid, replies)

    async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not q or not update.effective_user:
            return
        await q.answer()
        uid = update.effective_user.id
        try:
            replies = await commands.handle_callback(uid, update.effective_chat.id, q.data or "")
        except Exception:  # noqa: BLE001
            log.exception("callback failed")
            replies = [Reply("⚠️ Action failed.")]
        await _deliver(notifier, uid, replies)

    async def post_init(app: Application) -> None:
        notifier.app = app
        from telegram import BotCommand
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in [
            ("start", "Menu & balances"), ("status", "Status & data sources"), ("chain", "Active chain"),
            ("leaderboard", "Ranked traders"), ("follow", "Follow a trader"), ("positions", "Open positions"),
            ("buy", "Manual paper buy"), ("sell", "Manual paper sell"), ("close", "Close a position"),
            ("pnl", "PnL report"), ("pnlcard", "PnL card image"), ("card", "Trade card image"), ("log", "Trade log"), ("settings", "Settings"), ("groupbuy", "Group Buy mode"),
            ("bench", "Benched traders"), ("pause", "Pause"), ("resume", "Resume"), ("mode", "Paper/Live"),
            ("advisor", "Grok advisor"), ("export", "CSV export"), ("chart", "Equity chart"), ("hours", "Time-of-day stats"),
            ("help", "Help")]])
        runner.start()

    async def post_shutdown(app: Application) -> None:
        await runner.shutdown()
        await http.close()

    app = Application.builder().token(cfg.telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(MessageHandler(filters.TEXT, on_text))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
