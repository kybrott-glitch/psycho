"""
main.py — Guest-Mode Inline Bot (polling, no webhook).

Usage: python main.py
Users: @YourBot CODE  in any chat → bot sends the post into that chat.
Admins: DM the bot to manage posts.
"""

import asyncio
import logging
import re
import sys
from typing import Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    ChosenInlineResultHandler,
    CommandHandler,
    ConversationHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import config
from database import (
    bump_trigger,
    create_post,
    delete_post,
    get_post,
    get_post_any,
    init_db,
    list_posts,
    update_post,
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────────────────────
ASK_CODE, ASK_TEXT, ASK_IMAGE, ASK_BUTTONS, CONFIRM = range(5)

_wizard: Dict[int, dict] = {}

COLOR_MAP = {
    "red": "danger",
    "blue": "primary",
    "green": "success",
    "grey": None, "gray": None, "default": None,
}


# ── Helpers ────────────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_USER_IDS


def admin_only(func):
    async def wrapper(update: Update, context: CallbackContext):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Not authorised.")
            return ConversationHandler.END
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def build_raw_buttons(buttons: List[dict]) -> List[dict]:
    """Buttons as raw dicts with Telegram `style` field for color."""
    row = []
    for btn in buttons[:3]:
        color = btn.get("color", "default").lower()
        b = {"text": btn["text"], "url": btn["url"]}
        style = COLOR_MAP.get(color)
        if style:
            b["style"] = style
        row.append(b)
    return row


def make_markup(buttons: List[dict]) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    raw = build_raw_buttons(buttons)
    # Pass raw dicts; PTB serialises them as-is including `style`
    row = [InlineKeyboardButton(**{k: v for k, v in b.items() if k != "style"}) for b in raw]
    markup = InlineKeyboardMarkup([row])
    # Inject `style` into the serialised payload by patching _inline_keyboard
    for i, btn in enumerate(markup.inline_keyboard[0]):
        style = raw[i].get("style")
        if style:
            btn._kwargs = getattr(btn, "_kwargs", {})
            btn._kwargs["style"] = style
    return markup


# ── Inline query ───────────────────────────────────────────────────────────────
async def inline_handler(update: Update, context: CallbackContext) -> None:
    query = update.inline_query
    code = (query.query or "").strip().upper()

    if not code:
        await query.answer([], cache_time=0)
        return

    post = await get_post(code)
    if not post:
        await query.answer([], cache_time=5, switch_pm_text="❌ Code not found", switch_pm_parameter="help")
        return

    raw_buttons = build_raw_buttons(post.buttons)
    reply_markup = None
    if raw_buttons:
        from telegram import InlineKeyboardMarkup as IKM
        reply_markup = IKM.de_json({"inline_keyboard": [raw_buttons]}, context.bot)

    if post.image_url:
        from telegram import InlineQueryResultPhoto
        result = InlineQueryResultPhoto(
            id=post.code,
            photo_url=post.image_url,
            thumbnail_url=post.image_url,
            caption=post.text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    else:
        result = InlineQueryResultArticle(
            id=post.code,
            title=f"📌 {post.code}",
            description=re.sub(r"<[^>]+>", "", post.text)[:120],
            input_message_content=InputTextMessageContent(
                message_text=post.text,
                parse_mode=ParseMode.HTML,
            ),
            reply_markup=reply_markup,
        )

    await query.answer([result], cache_time=config.INLINE_CACHE_TIME, is_personal=False)


async def chosen_handler(update: Update, context: CallbackContext) -> None:
    code = update.chosen_inline_result.result_id
    await bump_trigger(code)
    logger.info("Post %s triggered by user %s", code, update.chosen_inline_result.from_user.id)


# ── /start ─────────────────────────────────────────────────────────────────────
async def start(update: Update, context: CallbackContext) -> None:
    if is_admin(update.effective_user.id):
        await update.message.reply_text(
            "👋 <b>Admin Panel</b>\n\n"
            "/addpost — create a post\n"
            "/editpost &lt;CODE&gt; — edit a post\n"
            "/listposts — view all posts\n"
            "/deletepost &lt;CODE&gt; — delete a post\n"
            "/togglepost &lt;CODE&gt; — activate / deactivate\n"
            "/stats &lt;CODE&gt; — trigger count",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "Use me inline: type <code>@YourBot CODE</code> in any chat.",
            parse_mode=ParseMode.HTML,
        )


# ── /addpost wizard ────────────────────────────────────────────────────────────
@admin_only
async def addpost_start(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    _wizard[uid] = {}
    if context.args:
        code = context.args[0].upper()
        _wizard[uid]["code"] = code
        await update.message.reply_text(f"Code: <b>{code}</b>\n\nSend the post <b>text</b> (HTML ok):", parse_mode=ParseMode.HTML)
        return ASK_TEXT
    await update.message.reply_text("Step 1/4 — Enter trigger <b>CODE</b> (letters/numbers/_ only):", parse_mode=ParseMode.HTML)
    return ASK_CODE


async def got_code(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    code = update.message.text.strip().upper()
    if not re.match(r"^[A-Z0-9_-]{1,64}$", code):
        await update.message.reply_text("❌ Invalid. Use letters, numbers, _ or -. Try again:")
        return ASK_CODE
    if await get_post(code):
        await update.message.reply_text(f"⚠️ <b>{code}</b> already exists. Use a different code:", parse_mode=ParseMode.HTML)
        return ASK_CODE
    _wizard[uid]["code"] = code
    await update.message.reply_text(f"✅ Code: <b>{code}</b>\n\nStep 2/4 — Send the post <b>text</b> (HTML ok):", parse_mode=ParseMode.HTML)
    return ASK_TEXT


async def got_text(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    _wizard[uid]["text"] = update.message.text
    await update.message.reply_text("Step 3/4 — Send an <b>image URL</b> or <code>skip</code>:", parse_mode=ParseMode.HTML)
    return ASK_IMAGE


async def got_image(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    val = update.message.text.strip()
    _wizard[uid]["image_url"] = None if val.lower() == "skip" else val
    await update.message.reply_text(
        "Step 4/4 — Add up to <b>3 buttons</b>, one per line:\n"
        "<code>Label | https://url | color</code>\n"
        "Colors: <code>blue</code> <code>red</code> <code>green</code> <code>grey</code>\n\n"
        "Or send <code>skip</code>.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_BUTTONS


async def got_buttons(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    val = update.message.text.strip()
    buttons = []
    if val.lower() != "skip":
        for line in val.splitlines()[:3]:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                continue
            color = parts[2].lower() if len(parts) >= 3 else "default"
            if color not in COLOR_MAP:
                color = "default"
            buttons.append({"text": parts[0], "url": parts[1], "color": color})
    _wizard[uid]["buttons"] = buttons

    w = _wizard[uid]
    btn_lines = "\n".join(f"  • {b['text']} → {b['url']} [{b['color']}]" for b in buttons) or "  (none)"
    await update.message.reply_text(
        f"<b>Confirm?</b>\n\n"
        f"Code: <code>{w['code']}</code>\n"
        f"Text: {w['text'][:200]}\n"
        f"Image: {w.get('image_url') or '—'}\n"
        f"Buttons:\n{btn_lines}\n\n"
        "Send <b>yes</b> to save or <b>cancel</b>.",
        parse_mode=ParseMode.HTML,
    )
    return CONFIRM


async def do_confirm(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    if update.message.text.strip().lower() != "yes":
        _wizard.pop(uid, None)
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END
    w = _wizard.pop(uid)
    try:
        post = await create_post(w["code"], w["text"], w.get("image_url"), w.get("buttons", []))
        await update.message.reply_text(f"✅ Post <b>{post.code}</b> created!", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END


async def do_cancel(update: Update, context: CallbackContext) -> int:
    _wizard.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


addpost_conv = ConversationHandler(
    entry_points=[CommandHandler("addpost", addpost_start)],
    states={
        ASK_CODE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_code)],
        ASK_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_text)],
        ASK_IMAGE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_image)],
        ASK_BUTTONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_buttons)],
        CONFIRM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, do_confirm)],
    },
    fallbacks=[CommandHandler("cancel", do_cancel)],
)


# ── /editpost wizard (reuses same steps) ──────────────────────────────────────
@admin_only
async def editpost_start(update: Update, context: CallbackContext) -> int:
    if not context.args:
        await update.message.reply_text("Usage: /editpost &lt;CODE&gt;", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    code = context.args[0].upper()
    uid = update.effective_user.id
    _wizard[uid] = {"code": code, "_edit": True}
    await update.message.reply_text(f"✏️ Editing <b>{code}</b>\n\nSend new <b>text</b>:", parse_mode=ParseMode.HTML)
    return ASK_TEXT


async def edit_confirm(update: Update, context: CallbackContext) -> int:
    uid = update.effective_user.id
    if update.message.text.strip().lower() != "yes":
        _wizard.pop(uid, None)
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END
    w = _wizard.pop(uid)
    try:
        await update_post(w["code"], text=w.get("text"), image_url=w.get("image_url"), buttons=w.get("buttons", []))
        await update.message.reply_text(f"✅ Post <b>{w['code']}</b> updated!", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END


editpost_conv = ConversationHandler(
    entry_points=[CommandHandler("editpost", editpost_start)],
    states={
        ASK_TEXT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_text)],
        ASK_IMAGE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_image)],
        ASK_BUTTONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_buttons)],
        CONFIRM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_confirm)],
    },
    fallbacks=[CommandHandler("cancel", do_cancel)],
)


# ── Simple admin commands ──────────────────────────────────────────────────────
@admin_only
async def listposts(update: Update, context: CallbackContext) -> None:
    posts = await list_posts()
    if not posts:
        await update.message.reply_text("No posts yet. Use /addpost.")
        return
    lines = []
    for p in posts:
        status = "✅" if p.is_active else "⏸"
        img = "🖼" if p.image_url else "📝"
        lines.append(f"{status} {img} <code>{p.code}</code> — {len(p.buttons)} btn(s) — {p.trigger_count}× sent")
    await update.message.reply_text("<b>Posts</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def deletepost(update: Update, context: CallbackContext) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /deletepost &lt;CODE&gt;", parse_mode=ParseMode.HTML)
        return
    code = context.args[0].upper()
    ok = await delete_post(code)
    await update.message.reply_text(
        f"🗑 <b>{code}</b> deleted." if ok else f"❌ <b>{code}</b> not found.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def togglepost(update: Update, context: CallbackContext) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /togglepost &lt;CODE&gt;", parse_mode=ParseMode.HTML)
        return
    code = context.args[0].upper()
    post = await get_post_any(code)
    if not post:
        await update.message.reply_text(f"❌ <b>{code}</b> not found.", parse_mode=ParseMode.HTML)
        return
    new = not post.is_active
    await update_post(code, is_active=new)
    await update.message.reply_text(
        f"Post <b>{code}</b> {'✅ activated' if new else '⏸ deactivated'}.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def stats(update: Update, context: CallbackContext) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /stats &lt;CODE&gt;", parse_mode=ParseMode.HTML)
        return
    code = context.args[0].upper()
    post = await get_post_any(code)
    if not post:
        await update.message.reply_text(f"❌ <b>{code}</b> not found.", parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        f"📊 <b>{code}</b>\nTriggered: <b>{post.trigger_count}</b>×\n"
        f"Active: {'Yes' if post.is_active else 'No'}\n"
        f"Created: {post.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
        parse_mode=ParseMode.HTML,
    )


# ── Main ───────────────────────────────────────────────────────────────────────
async def main() -> None:
    await init_db()

    app = ApplicationBuilder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(addpost_conv)
    app.add_handler(editpost_conv)
    app.add_handler(CommandHandler("listposts", listposts))
    app.add_handler(CommandHandler("deletepost", deletepost))
    app.add_handler(CommandHandler("togglepost", togglepost))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(InlineQueryHandler(inline_handler))
    app.add_handler(ChosenInlineResultHandler(chosen_handler))

    logger.info("Bot started (polling)…")
    await app.run_polling(allowed_updates=["message", "inline_query", "chosen_inline_result"])


if __name__ == "__main__":
    asyncio.run(main())
