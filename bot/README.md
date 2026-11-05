# UZEX AI Analyst — Telegram bot

This project implements the Telegram interface described in the UZEX AI Hackathon bot specification. The bot is a thin client: it sends every text question to the team's API and displays the API response and evidence. It does not query the trade database or invent registry facts.

## Features

- Aiogram 3.x long polling; no Telegram webhook, user installation, registration, or CAPTCHA.
- `/start` in Russian and Uzbek, with four inline example-question buttons.
- `/help` in RU+UZ with eight example questions and the stated registry date range.
- Every text message up to 1,000 characters is sent to `POST {API_URL}/ask` as `{ "question": "...", "user_id": "..." }`.
- `API_TIMEOUT` defaults to 85 seconds. While waiting, the bot edits a placeholder when the response arrives and keeps the typing indicator active.
- `status=ok`, `no_data`, and `error` are displayed distinctly. `verified=false` uses `☑️`, not `✅`.
- Answer text is HTML-escaped before the API's documented `**bold**` syntax is converted to Telegram HTML.
- Evidence is cached in an LRU of 1,000 entries and shown via `🔍 Доказательства / Dalillar`; CSV is sent as UTF-8 with BOM.
- Evidence view shows SQL and at most 10 data rows per block; numeric values are formatted with spaces between thousands.
- Voice messages, stickers and photos receive the bilingual text-only notice.
- Questions are handled concurrently. Questions, status, elapsed time and the first 300 answer characters are appended to `logs/qa.jsonl`.

## 1. Requirements

Use Python 3.11+ (Python 3.13 is recommended for the current Windows environment), then install dependencies in a virtual environment.

### Windows PowerShell / VS Code

```powershell
cd "C:\Users\YOUR_USER\Downloads\uzex_telegram_bot\uzex_telegram_bot"
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Set the token received from `@BotFather` in `.env`:

```env
BOT_TOKEN=YOUR_BOTFATHER_TOKEN
API_URL=http://127.0.0.1:8000
API_TIMEOUT=85
LOG_LEVEL=INFO
```

Run the bot:

```powershell
python bot.py
```

Keep the terminal open while testing. Stop local polling with `Ctrl+C`.

## 2. API contract (do not change without agreement)

The bot sends:

```http
POST {API_URL}/ask
Content-Type: application/json
```

```json
{
  "question": "Сколько сделок по карбамиду было в 2025 году?",
  "user_id": "123456789"
}
```

Expected response:

```json
{
  "answer": "В 2025 году по карбамиду заключено **1 234** сделки ...",
  "status": "ok",
  "lang": "ru",
  "verified": true,
  "elapsed_ms": 5400,
  "evidence": [
    {
      "title": "Число сделок по карбамиду за 2025 год",
      "sql": "SELECT COUNT(*) AS deals FROM deals WHERE product = 'Карбамид' AND year = 2025",
      "row_count": 1,
      "columns": ["deals"],
      "rows": [[1234]]
    }
  ]
}
```

Supported `status`: `ok`, `no_data`, `error`. Supported `lang`: `ru`, `uz`, `uz-cyrl`.

Health endpoint expected from the real API: `GET {API_URL}/health` → `{"status":"ok"}`. The bot does not require health endpoint availability to start polling.

For backward compatibility, old `.env` names `BACKEND_URL` and `REQUEST_TIMEOUT` are accepted, but new deployments should use `API_URL` and `API_TIMEOUT`.

## 3. Local mock API (for UI testing only)

The supplied task includes a temporary mock so the Telegram interface can be tested before the real AI/SQL API is ready. Its values are illustrative and are **not actual UZEX registry data**. Never use it for the commission demo or production.

Install mock dependencies:

```powershell
pip install -r requirements-mock.txt
```

In terminal 1, start mock API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn mock:app --host 127.0.0.1 --port 8000
```

In terminal 2, start Telegram bot:

```powershell
.\.venv\Scripts\python.exe bot.py
```

Test `GET http://127.0.0.1:8000/health` in a browser. Ask a 2025 question to see the illustrative `ok` + evidence response, and ask about 2021 to see `no_data`.

## 4. BotFather settings

Suggested display name: `UZEX AI Analyst`.

Description / About (RU+UZ):

`ИИ-аналитик реестра сделок UZEX. Отвечает только по данным. / UZEX bitimlari reyestri bo‘yicha AI tahlilchi. Faqat ma’lumotlarga asoslanib javob beradi.`

Commands:

```text
start - Запустить бота / Botni ishga tushirish
help - Помощь и примеры / Yordam va misollar
```

## 5. Logs and deployment notes

- QA log: `logs/qa.jsonl` (created automatically).
- Do not commit `.env`; `.gitignore` excludes it.
- Run one polling process per bot token only.
- For production, deploy on an always-on VPS or Railway service that does not sleep. Use `systemd` or Docker restart policy; keep API and bot running after reboot.
- Before the commission demo, replace `API_URL` with the real team API URL and verify the API's `/health` and `/ask` endpoints. Do not leave the mock API running.
