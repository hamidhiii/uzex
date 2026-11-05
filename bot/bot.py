"""UZEX AI Analyst Telegram frontend.

The bot is deliberately a thin client: all factual answers and SQL evidence
come from the team's API. It never calculates or invents registry data.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import secrets
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# BACKEND_URL/REQUEST_TIMEOUT are accepted as migration aliases for older .env files.
API_URL = os.getenv("API_URL", os.getenv("BACKEND_URL", "http://127.0.0.1:8000")).strip().rstrip("/")
API_TIMEOUT = float(os.getenv("API_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "85")))
MAX_QUESTION_LENGTH = 1000
TELEGRAM_SAFE_LENGTH = 3900
EVIDENCE_CACHE_SIZE = 1000

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("uzex-telegram-bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to the .env file.")

# Evidence is kept in memory because Telegram callback_data is limited to 64 bytes.
_evidence_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_evidence_lock = asyncio.Lock()
_log_lock = asyncio.Lock()
_http: httpx.AsyncClient | None = None

EXAMPLES: list[tuple[str, str]] = [
    ("Топ-5 покупателей цемента в 2025 году", "Топ-5 покупателей цемента в 2025 году"),
    ("Какой срок оплаты по контракту 101429?", "Какой срок оплаты по контракту 101429?"),
    ("2024-yilda karbamid bo‘yicha nechta bitim tuzilgan?", "2024-yilda karbamid bo‘yicha nechta bitim tuzilgan?"),
    ("BK045 brokeri qaysi viloyatdan?", "BK045 brokeri qaysi viloyatdan?"),
]

TEXTS = {
    "ru": {
        "placeholder": "⏳ Считаю по данным…",
        "error": "⚠️ Не удалось получить ответ, попробуйте ещё раз.",
        "too_long": "Вопрос слишком длинный. Отправьте не более 1000 символов.",
        "empty": "Отправьте текстовый вопрос о реестре сделок.",
        "non_text": "Пока понимаю только текст / Hozircha faqat matnni tushunaman.",
        "evidence": "🔍 Доказательства",
        "csv": "📥 CSV",
        "rows": "Строк в результате",
        "continuation": "Продолжение",
        "csv_caption": "Доказательства по вашему запросу.",
    },
    "uz": {
        "placeholder": "⏳ Ma’lumotlar bo‘yicha hisoblayapman…",
        "error": "⚠️ Javob olib bo‘lmadi, qayta urinib ko‘ring.",
        "too_long": "Savol juda uzun. 1000 belgidan oshmagan matn yuboring.",
        "empty": "Savdo reyestri bo‘yicha matnli savol yuboring.",
        "non_text": "Hozircha faqat matnni tushunaman.",
        "evidence": "🔍 Dalillar",
        "csv": "📥 CSV",
        "rows": "Natijadagi satrlar",
        "continuation": "Davomi",
        "csv_caption": "So‘rovingiz bo‘yicha dalillar.",
    },
    "uz-cyrl": {
        "placeholder": "⏳ Маълумотлар бўйича ҳисоблаяпман…",
        "error": "⚠️ Жавоб олиб бўлмади, қайта уриниб кўринг.",
        "too_long": "Савол жуда узун. 1000 белгидан ошмаган матн юборинг.",
        "empty": "Савдо реестри бўйича матнли савол юборинг.",
        "non_text": "Ҳозирча фақат матнни тушунаман.",
        "evidence": "🔍 Далиллар",
        "csv": "📥 CSV",
        "rows": "Натижадаги қаторлар",
        "continuation": "Давоми",
        "csv_caption": "Сўровингиз бўйича далиллар.",
    },
}


def detect_language(text: str, telegram_language: str | None = None) -> str:
    """Best-effort language guess for the short placeholder/error messages.

    The final answer and its service labels use the authoritative API `lang`.
    """
    value = (text or "").lower()
    if re.search(r"[ўқғҳҷ]", value):
        return "uz-cyrl"
    if re.search(r"[а-яё]", value):
        # Cyrillic Uzbek without Uzbek-specific letters is ambiguous; use common words.
        uz_cyrillic = ("қайси", "нечта", "бўйича", "йилда", "битим", "сотувчи", "вилоят")
        if any(word in value for word in uz_cyrillic):
            return "uz-cyrl"
        return "ru"
    uz_words = (
        "qaysi", "nechta", "bo‘yicha", "bo'yicha", "yilda", "bitim", "sotuvchi",
        "viloyat", "shartnoma", "xarid", "sotib", "brokeri", "hisobla",
    )
    if any(word in value for word in uz_words):
        return "uz"
    if telegram_language and telegram_language.lower().startswith("ru"):
        return "ru"
    return "uz"


def lang_key(value: Any, fallback: str = "uz") -> str:
    candidate = str(value or "").lower()
    if candidate in TEXTS:
        return candidate
    if candidate.startswith("uz-cyrl") or candidate.startswith("uz_cyrl"):
        return "uz-cyrl"
    if candidate.startswith("ru"):
        return "ru"
    if candidate.startswith("uz"):
        return "uz"
    return fallback if fallback in TEXTS else "uz"


def markdown_bold_to_html(text: str) -> str:
    """Escape all backend text, then allow only its documented **bold** syntax."""
    escaped = html.escape(text, quote=True)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)


def split_text(text: str, limit: int) -> list[str]:
    """Split plain text into chunks without losing characters."""
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def split_answer(answer: str, limit: int = 550) -> list[str]:
    """Split an answer while keeping markdown bold markers inside each chunk."""
    # API answers are normally short. Paragraph-first splitting reduces the
    # chance of separating a **bold phrase** across Telegram messages.
    paragraphs = answer.split("\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else current + "\n" + paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) > limit:
            chunks.extend(split_text(paragraph, limit))
        else:
            current = paragraph
    if current or not chunks:
        chunks.append(current)
    return chunks


def format_number(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return "—" if value is None else str(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}".replace(",", " ")
        integer, fractional = f"{value:,.12f}".rstrip("0").rstrip(".").split(".")
        return integer.replace(",", " ") + "." + fractional
    if isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip()):
        raw = value.strip()
        if "." in raw:
            integer, fraction = raw.split(".", 1)
            return f"{int(integer):,}".replace(",", " ") + "." + fraction
        return f"{int(raw):,}".replace(",", " ")
    return str(value)


_ID_COLUMN = re.compile(r"(?:^|_)(?:id|inn|stir|code|year|hs_code|broker)$|_broker$|^(?:deal|contract)_id$", re.I)


def format_cell(column: str, value: Any) -> str:
    """ИНН, номера сделок/контрактов, коды и годы — без пробелов-разделителей тысяч."""
    if _ID_COLUMN.search(column or "") and value is not None and not isinstance(value, (bool, float)):
        return str(value)
    if isinstance(value, str):  # API отдаёт числа числами; строки (ИНН, даты) показываем как есть
        return value
    return format_number(value)


def validate_evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("API evidence must be a list")
    clean: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        columns = item.get("columns", [])
        rows = item.get("rows", [])
        if not isinstance(columns, list):
            columns = []
        if not isinstance(rows, list):
            rows = []
        clean.append({
            "title": str(item.get("title") or "Доказательство"),
            "sql": str(item.get("sql") or ""),
            "row_count": item.get("row_count", len(rows)),
            "columns": [str(c) for c in columns],
            "rows": rows,
        })
    return clean


async def cache_evidence(evidence: list[dict[str, Any]]) -> str:
    async with _evidence_lock:
        short_id = secrets.token_hex(4)
        while short_id in _evidence_cache:
            short_id = secrets.token_hex(4)
        _evidence_cache[short_id] = evidence
        _evidence_cache.move_to_end(short_id)
        while len(_evidence_cache) > EVIDENCE_CACHE_SIZE:
            _evidence_cache.popitem(last=False)
        return short_id


async def get_cached_evidence(short_id: str) -> list[dict[str, Any]] | None:
    async with _evidence_lock:
        evidence = _evidence_cache.get(short_id)
        if evidence is not None:
            _evidence_cache.move_to_end(short_id)
        return evidence


def result_keyboard(short_id: str, lang: str) -> InlineKeyboardMarkup:
    labels = TEXTS[lang]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=labels["evidence"], callback_data=f"ev:{short_id}"),
        InlineKeyboardButton(text=labels["csv"], callback_data=f"csv:{short_id}"),
    ]])


async def ask_backend(question: str, user_id: str) -> dict[str, Any]:
    if _http is None:
        raise RuntimeError("HTTP client is not initialized")
    response = await _http.post(
        f"{API_URL}/ask",
        json={"question": question, "user_id": user_id},
        timeout=httpx.Timeout(API_TIMEOUT, connect=8.0),
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("API response must be a JSON object")
    status = payload.get("status")
    if status not in {"ok", "no_data", "error"}:
        raise ValueError("API returned an unknown status")
    if status in {"ok", "no_data"}:
        if not isinstance(payload.get("answer"), str) or not payload["answer"].strip():
            raise ValueError("API response has no answer")
    payload["evidence"] = validate_evidence(payload.get("evidence", []))
    return payload


async def write_qa_log(
    user_id: str,
    question: str,
    status: str,
    elapsed_ms: int,
    answer: str = "",
) -> None:
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "question": question,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "answer_preview": answer[:300],
    }
    async with _log_lock:
        os.makedirs("logs", exist_ok=True)
        with open(os.path.join("logs", "qa.jsonl"), "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


async def edit_html_or_plain(message: Message, html_text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(html_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as exc:
        # HTML parse/entity failures must not cause a lost answer.
        if "parse" not in str(exc).lower() and "entity" not in str(exc).lower():
            raise
        plain_text = html.unescape(re.sub(r"</?(?:b|i|code|pre)\b[^>]*>", "", html_text))
        await message.edit_text(plain_text, reply_markup=reply_markup, parse_mode=None)


async def send_html_or_plain(message: Message, html_text: str, reply_to_message_id: int) -> None:
    try:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=html_text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest as exc:
        if "parse" not in str(exc).lower() and "entity" not in str(exc).lower():
            raise
        plain_text = html.unescape(re.sub(r"</?(?:b|i|code|pre)\b[^>]*>", "", html_text))
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=plain_text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=None,
        )


async def typing_loop(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            log.debug("Could not send typing action", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


def status_prefix(status: str, verified: Any) -> str:
    if status == "no_data":
        return "ℹ️ "
    if status == "error":
        return "⚠️ "
    return "✅ " if verified is True else "☑️ "


def elapsed_line(payload: dict[str, Any], measured_ms: int) -> str:
    value = payload.get("elapsed_ms", measured_ms)
    try:
        elapsed = max(0, int(value)) / 1000
    except (TypeError, ValueError):
        elapsed = max(0, measured_ms) / 1000
    return f"\n\n<code>⏱ {elapsed:.1f} с</code>"


async def handle_question(
    message: Message,
    question: str,
    user_id: str,
    fallback_lang: str,
) -> None:
    question = (question or "").strip()
    fallback_lang = lang_key(fallback_lang)
    labels = TEXTS[fallback_lang]
    if not question:
        await message.answer(labels["empty"])
        return
    if len(question) > MAX_QUESTION_LENGTH:
        await message.answer(labels["too_long"])
        return

    started = asyncio.get_running_loop().time()
    placeholder = await message.answer(labels["placeholder"])
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(typing_loop(message.bot, message.chat.id, stop_typing))
    payload: dict[str, Any] | None = None
    try:
        payload = await ask_backend(question, user_id)
        measured_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        response_lang = lang_key(payload.get("lang"), fallback_lang)
        labels = TEXTS[response_lang]
        status_value = payload["status"]

        if status_value == "error":
            answer = labels["error"]
            keyboard = None
            html_parts = [status_prefix("error", False) + markdown_bold_to_html(answer)]
            html_parts[0] += elapsed_line(payload, measured_ms)
        else:
            answer = payload["answer"].strip()
            evidence = payload["evidence"]
            short_id = await cache_evidence(evidence) if evidence else None
            keyboard = result_keyboard(short_id, response_lang) if short_id else None
            answer_chunks = split_answer(answer)
            html_parts = [status_prefix(status_value, payload.get("verified")) + markdown_bold_to_html(answer_chunks[0])]
            html_parts[0] += elapsed_line(payload, measured_ms)
            for chunk in answer_chunks[1:]:
                html_parts.append(f"<b>{labels['continuation']}:</b>\n" + markdown_bold_to_html(chunk))

        # Edit the original placeholder, then send any answer continuations as replies.
        await edit_html_or_plain(placeholder, html_parts[0], keyboard)
        for part in html_parts[1:]:
            for subpart in split_text(part, TELEGRAM_SAFE_LENGTH - 40):
                await send_html_or_plain(message, subpart, placeholder.message_id)

        await write_qa_log(
            user_id=user_id,
            question=question,
            status=status_value,
            elapsed_ms=int(payload.get("elapsed_ms", measured_ms) or measured_ms),
            answer=answer,
        )
    except (httpx.TimeoutException, asyncio.TimeoutError):
        measured_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        await edit_html_or_plain(placeholder, labels["error"])
        await write_qa_log(user_id, question, "timeout", measured_ms)
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        measured_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        log.warning("API request failed: %s", type(exc).__name__)
        await edit_html_or_plain(placeholder, labels["error"])
        await write_qa_log(user_id, question, "error", measured_ms)
    except Exception:
        measured_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        log.exception("Unexpected error while answering a question")
        try:
            await edit_html_or_plain(placeholder, labels["error"])
        finally:
            await write_qa_log(user_id, question, "error", measured_ms)
    finally:
        stop_typing.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=EXAMPLES[0][1], callback_data="ex:0")],
        [InlineKeyboardButton(text=EXAMPLES[1][1], callback_data="ex:1")],
        [InlineKeyboardButton(text=EXAMPLES[2][1], callback_data="ex:2")],
        [InlineKeyboardButton(text=EXAMPLES[3][1], callback_data="ex:3")],
    ])


async def cmd_start(message: Message) -> None:
    text = (
        "👋 Assalomu alaykum! Здравствуйте!\n\n"
        "Я ИИ-аналитик реестра сделок UZEX: 500 000 сделок, 2022–2026. "
        "Отвечаю только по данным. Если ответа в данных нет — так и скажу.\n\n"
        "Birja bitimlari bo‘yicha savol bering — o‘zbek yoki rus tilida. "
        "Задайте вопрос о сделках биржи на русском или узбекском."
    )
    await message.answer(text, reply_markup=start_keyboard())


async def cmd_help(message: Message) -> None:
    text = (
        "<b>Как пользоваться / Foydalanish</b>\n\n"
        "Отправьте вопрос обычным текстом на русском или узбекском. "
        "Бот показывает ответ, SQL-запрос и строки, на которых основан ответ. "
        "Если в реестре нет данных, бот сообщит об этом.\n\n"
        "Oddiy matn ko‘rinishida ruscha yoki o‘zbekcha savol yuboring. "
        "Bot javobni, SQL so‘rovini va javobga asos bo‘lgan satrlarni ko‘rsatadi. "
        "Reyestrda ma’lumot bo‘lmasa, bu haqda ochiq xabar beradi.\n\n"
        "<b>Примеры / Misollar</b>\n"
        "1. Топ-5 покупателей цемента в 2025 году\n"
        "2. Какой срок оплаты по контракту 101429?\n"
        "3. Сколько сделок по карбамиду было в марте 2025?\n"
        "4. Из какого региона брокер BK045?\n"
        "5. Кто купил больше всего цемента в 2024 году?\n"
        "6. 2024-yilda karbamid bo‘yicha nechta bitim tuzilgan?\n"
        "7. 986932945 STIR raqamli sotuvchi qaysi viloyatdan?\n"
        "8. 2025-yilda eng ko‘p sement sotib olgan kompaniya qaysi?\n\n"
        "<i>Данные реестра / Reyestr ma’lumotlari: 03.01.2022–30.10.2026.</i>"
    )
    await message.answer(text)


async def handle_text(message: Message) -> None:
    question = (message.text or "").strip()
    language_code = message.from_user.language_code if message.from_user else None
    fallback = detect_language(question, language_code)
    if not question:
        await message.answer(TEXTS[fallback]["empty"])
        return
    if len(question) > MAX_QUESTION_LENGTH:
        await message.answer(TEXTS[fallback]["too_long"])
        return
    user_id = str(message.from_user.id) if message.from_user else "unknown"
    await handle_question(message, question, user_id, fallback)


async def handle_example(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        index = int((callback.data or "").split(":", 1)[1])
        question = EXAMPLES[index][0]
    except (ValueError, IndexError):
        return
    if not callback.message:
        return
    # Put the selected example into the chat, then process it like a normal question.
    question_message = await callback.message.answer(question)
    user_id = str(callback.from_user.id)
    fallback = detect_language(question, callback.from_user.language_code)
    await handle_question(question_message, question, user_id, fallback)


def format_evidence_block(block: dict[str, Any], index: int, lang: str) -> list[str]:
    labels = TEXTS[lang]
    title = str(block.get("title") or f"Evidence {index}")
    sql = str(block.get("sql") or "")
    columns = [str(c) for c in block.get("columns", [])]
    rows = block.get("rows", [])
    if not isinstance(rows, list):
        rows = []
    title_parts = split_text(title, 500)
    out = [f"<b>{index}. {html.escape(title_parts[0])}</b>"]
    out.extend(html.escape(part) for part in title_parts[1:])
    if sql:
        # SQL is split before escaping so every part remains a valid HTML entity sequence.
        for sql_part in split_text(sql, 500):
            out.append(f"<pre><code class=\"language-sql\">{html.escape(sql_part)}</code></pre>")
    out.append(f"{labels['rows']}: {html.escape(format_number(block.get('row_count', len(rows))))}")

    if not columns or not rows:
        out.append("<i>0 rows</i>")
        return out

    shown = rows[:10]
    normalized_rows: list[list[str]] = []
    for row in shown:
        if isinstance(row, dict):
            normalized_rows.append([format_cell(col, row.get(col)) for col in columns])
        elif isinstance(row, (list, tuple)):
            normalized_rows.append([format_cell(columns[i], row[i]) if i < len(row) else "—" for i in range(len(columns))])
        else:
            normalized_rows.append([format_number(row)] + ["—"] * (len(columns) - 1))

    widths = [len(c) for c in columns]
    for row in normalized_rows:
        for col_index, value in enumerate(row):
            widths[col_index] = max(widths[col_index], len(value))
    total_width = sum(widths) + (3 * max(0, len(columns) - 1))

    if len(columns) > 4 or total_width > 60:
        for row_index, row in enumerate(normalized_rows, 1):
            out.append(f"<b>#{row_index}</b>")
            for col, value in zip(columns, row):
                # Each field is escaped independently; long values are continued safely.
                value_parts = split_text(value, 500)
                if len(value_parts) == 1:
                    out.append(f"{html.escape(col)}: {html.escape(value)}")
                else:
                    out.append(f"{html.escape(col)}:")
                    out.extend(html.escape(part) for part in value_parts)
    else:
        header = " | ".join(col.ljust(widths[i]) for i, col in enumerate(columns))
        separator = "-+-".join("-" * width for width in widths)
        table_lines = [header, separator]
        for row in normalized_rows:
            table_lines.append(" | ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
        for chunk in split_text("\n".join(table_lines), 500):
            out.append(f"<pre>{html.escape(chunk)}</pre>")
    if len(rows) > len(shown):
        out.append(f"… {len(rows) - len(shown)} more rows omitted (showing 10).")
    return out


def keyboard_language(message: Message) -> str:
    buttons = [
        button.text
        for row in (message.reply_markup.inline_keyboard if message.reply_markup else [])
        for button in row
    ]
    joined = " ".join(buttons)
    if "Dalillar" in joined:
        return "uz"
    if "Далиллар" in joined:
        return "uz-cyrl"
    return "ru"


async def send_evidence(callback: CallbackQuery, short_id: str) -> None:
    if not callback.message:
        return
    lang = keyboard_language(callback.message)
    evidence = await get_cached_evidence(short_id)
    if evidence is None:
        await callback.answer(
            "Dalillar endi mavjud emas. Savolni qayta yuboring." if lang == "uz" else
            "Далиллар энди мавжуд эмас. Саволни қайта юборинг." if lang == "uz-cyrl" else
            "Данные доказательств уже недоступны. Повторите вопрос.",
            show_alert=True,
        )
        return
    await callback.answer()

    # Each evidence block is its own reply to the answer. Split only between
    # complete safe HTML fragments so a code/table tag is never left unclosed.
    for index, block in enumerate(evidence, 1):
        fragments = format_evidence_block(block, index, lang)
        current = ""
        for fragment in fragments:
            candidate = fragment if not current else current + "\n" + fragment
            if len(candidate) <= TELEGRAM_SAFE_LENGTH:
                current = candidate
            else:
                if current:
                    await send_html_or_plain(callback.message, current, callback.message.message_id)
                current = fragment
        if current:
            await send_html_or_plain(callback.message, current, callback.message.message_id)


async def handle_evidence_callback(callback: CallbackQuery) -> None:
    short_id = (callback.data or "").split(":", 1)[1]
    await send_evidence(callback, short_id)


def create_csv(evidence: list[dict[str, Any]]) -> bytes:
    all_columns: list[str] = []
    for block in evidence:
        for column in block.get("columns", []):
            name = str(column)
            if name not in all_columns:
                all_columns.append(name)

    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["evidence_no", "title", "sql", "row_no", *all_columns])
    for evidence_no, block in enumerate(evidence, 1):
        columns = [str(c) for c in block.get("columns", [])]
        rows = block.get("rows", [])
        if not isinstance(rows, list) or not rows:
            writer.writerow([evidence_no, block.get("title", ""), block.get("sql", ""), "", *([""] * len(all_columns))])
            continue
        for row_no, row in enumerate(rows, 1):
            if isinstance(row, dict):
                values_by_column = {str(key): value for key, value in row.items()}
            elif isinstance(row, (list, tuple)):
                values_by_column = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
            else:
                values_by_column = {columns[0]: row} if columns else {}
            values = [values_by_column.get(col, "") for col in all_columns]
            writer.writerow([evidence_no, block.get("title", ""), block.get("sql", ""), row_no, *values])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


async def handle_csv_callback(callback: CallbackQuery) -> None:
    short_id = (callback.data or "").split(":", 1)[1]
    evidence = await get_cached_evidence(short_id)
    if not callback.message:
        return
    lang = keyboard_language(callback.message)
    if evidence is None:
        missing = "CSV dalillari endi mavjud emas. Savolni qayta yuboring." if lang == "uz" else (
            "CSV далиллари энди мавжуд эмас. Саволни қайта юборинг." if lang == "uz-cyrl" else
            "Данные CSV уже недоступны. Повторите вопрос."
        )
        await callback.answer(missing, show_alert=True)
        return
    await callback.answer()
    data = create_csv(evidence)
    file = BufferedInputFile(data, filename=f"evidence_{short_id}.csv")
    caption = "📥 Dalillar CSV fayli" if lang == "uz" else (
        "📥 Далиллар CSV файли" if lang == "uz-cyrl" else "📥 CSV с доказательствами"
    )
    await callback.message.answer_document(file, caption=caption)


async def handle_non_text(message: Message) -> None:
    language_code = message.from_user.language_code if message.from_user else None
    lang = "ru" if language_code and language_code.lower().startswith("ru") else "uz"
    await message.answer(TEXTS[lang]["non_text"])


async def main() -> None:
    global _http
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(API_TIMEOUT, connect=8.0),
        headers={"Accept": "application/json"},
    )
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(handle_text, F.text)
    dp.message.register(handle_non_text, F.voice | F.sticker | F.photo)
    dp.callback_query.register(handle_example, F.data.startswith("ex:"))
    dp.callback_query.register(handle_evidence_callback, F.data.startswith("ev:"))
    dp.callback_query.register(handle_csv_callback, F.data.startswith("csv:"))

    log.info("Bot starting; API=%s; timeout=%ss", API_URL, API_TIMEOUT)
    try:
        await dp.start_polling(bot, handle_as_tasks=True)
    finally:
        await bot.session.close()
        await _http.aclose()
        _http = None


if __name__ == "__main__":
    asyncio.run(main())
