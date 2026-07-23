"""Telegram bot for MedicalRAG."""
import asyncio
import os
import tempfile

from medrag.config import CFG
from medrag.rag.engine import RagEngine

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or CFG.get("telegram", {}).get("token", "")
TG_LIMIT = 4000
CLINICAL_HINTS = ("ecg", "ekg", "x-ray", "xray", "ct", "mri", "interpret", "clinical",
                  "نوار قلب", "رادیوگرافی")

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = RagEngine()
    return _engine


def _fmt_sources(sources):
    return "\n".join(f"[{s['n']}] {s.get('title', '?')} p{s.get('page', '?')}" for s in sources)


def _clip(text):
    return text if len(text) <= TG_LIMIT else text[:TG_LIMIT] + "\n…(truncated)"


def _answer_text(q):
    res = get_engine().answer(q)
    return _clip(f"{res['answer']}\n\n— Sources —\n{_fmt_sources(res['sources'])}")


def _answer_clinical(path, caption):
    res = get_engine().answer(caption or "Interpret this medical image.", image_path=path)
    head = f"{res['image_desc']}\n\n" if res.get("image_desc") else ""
    return _clip(f"{head}{res['answer']}\n\n— Sources —\n{_fmt_sources(res['sources'])}")


def _answer_qimage(path):
    out = get_engine().answer_question_image(path)
    parts = [f"Q{i}: {r['question'][:200]}\n{r['answer']}\n— Sources —\n{_fmt_sources(r['sources'])}"
             for i, r in enumerate(out["results"], 1)]
    return _clip("\n\n".join(parts) or "No question text detected.")


async def start(update, ctx):
    await update.message.reply_text(
        "Medical study assistant. Send a question or exam/clinical image.")


async def on_text(update, ctx):
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    reply = await asyncio.to_thread(_answer_text, update.message.text)
    await update.message.reply_text(reply)


async def on_photo(update, ctx):
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    photo = update.message.photo[-1]
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    tf.close()
    f = await ctx.bot.get_file(photo.file_id)
    await f.download_to_drive(tf.name)
    caption = (update.message.caption or "").strip()
    clinical = any(h in caption.lower() for h in CLINICAL_HINTS)
    try:
        reply = await asyncio.to_thread(
            _answer_clinical if clinical else _answer_qimage, tf.name,
            caption if clinical else None)
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass
    await update.message.reply_text(reply)


def main():
    if not TOKEN:
        raise SystemExit("Set telegram.token in config.yaml or TELEGRAM_BOT_TOKEN")
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    get_engine()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
