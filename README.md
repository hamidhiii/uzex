# UZEX AI Hackathon 2026 — агент по реестру сделок (ядро)

Агент отвечает на вопросы о биржевых сделках на русском и узбекском (латиница и кириллица) **только по данным**:
вопрос → SQL к DuckDB → проверка каждого числа в ответе → ответ + доказательства (SQL и строки).

## Быстрый старт

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # вписать LLM_API_KEY
# база: data/uzex.duckdb от участника 2, или собрать fallback из CSV в data/
.venv/bin/python scripts/build_db_fallback.py
.venv/bin/uvicorn app.api:app --port 8000
```

Открыть http://localhost:8000 — веб-интерфейс (чат + панель «Доказательства / Dalillar»).

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question":"Сколько сделок по карбамиду было в 2025 году?","user_id":"1"}'
```

CLI без сервера: `.venv/bin/python -m app.agent "BK045 brokeri qaysi viloyatdan?"`

## API (контракт с ботом)

`POST /ask {"question": str, "user_id": str|null}` →
`{"answer", "status": "ok"|"no_data"|"error", "lang": "ru"|"uz"|"uz-cyrl", "verified", "elapsed_ms", "evidence": [{"title","sql","row_count","columns","rows"}]}`.
Всегда HTTP 200; ошибки — `status: "error"` с вежливым текстом на языке вопроса. `GET /health → {"status":"ok","deals":500000}`.

У элементов `evidence` есть доп. поле `kind`: `sql` (запрос к базе) или `resolve` (поиск товара/компании/брокера/региона в справочниках).

## Переменные окружения (`.env`)

| Переменная | Пример |
|---|---|
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` (или Anthropic / OpenAI) |
| `LLM_API_KEY` | ключ провайдера |
| `LLM_MODEL` | `anthropic/claude-sonnet-5.5` |
| `LLM_FALLBACK_MODELS` | `anthropic/claude-sonnet-5,anthropic/claude-opus-5.5` — запасные при 429/5xx/таймауте |
| `LLM_RPM` | `18` для нового аккаунта OpenRouter; `0` — без лимита (Anthropic, OpenAI) |
| `LLM_PROMPT_CACHE` | `auto` — кэш системного промпта для Claude через OpenRouter (в 3–4 раза дешевле) |
| `PORT` | `8000` |
| `DB_PATH` | по умолчанию `data/uzex.duckdb` |

## База

- `data/uzex.duckdb` — рабочая база (сборка `scripts/build_db_fallback.py`, сверена с pandas-эталоном).
- `data/uzex_p2.duckdb` — база участника 2 (`build_db.py` из UZEX_DATABASE). Схема и значения совпадают с нашей,
  кроме одного: у пшеницы с `IV класс` (7 025 сделок, 48 контрактов) в ней `product_class = NULL`, у нас `4`.
  Переключение: `DB_PATH=data/uzex_p2.duckdb`.
- Синонимы товаров и алиасы регионов агент берёт и из базы, и из `app/normalize.py`, поэтому поиск одинаков на обеих базах.
- Справочник компаний агент строит сам из `participants.name` той же функцией `name_key`, что и для вопроса.
- Проверка: `python scripts/build_db_fallback.py --check`.

## Тесты и оценка

```bash
.venv/bin/python -m pytest -q tests                 # SQL-защита, resolve, lang, guard, verify
.venv/bin/python eval/gen_questions.py              # шаблонные вопросы, эталон — pandas по CSV
.venv/bin/python eval/run_eval.py                   # все eval/questions*.jsonl -> eval/report.md
.venv/bin/python eval/run_eval.py --model anthropic/claude-haiku-5.5 --fallback ''   # дешёвая отладка (~$0.05)
.venv/bin/python eval/run_eval.py --url http://HOST:8000/ask --only trap
```

## Telegram-бот (участник 1)

`bot/` — тонкий клиент на aiogram 3: шлёт вопрос в `POST {API_URL}/ask`, показывает ответ, кнопки «Доказательства» и «CSV».
Токен — в `bot/.env` (шаблон `bot/.env.example`, файл в git не попадает). Локально:

```bash
cd bot && python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python bot.py
```

Один процесс на один токен: если бот уже запущен у участника 1, второй экземпляр будет конфликтовать.

## Деплой

```bash
docker compose up -d --build      # agent (API) + bot; restart: always, data/ только чтение, логи в logs/
```

В compose бот ходит в API по внутреннему адресу `http://agent:8000`.

На VPS (Ubuntu/Debian, доступ по SSH-ключу) — одной командой с Mac:

```bash
scripts/deploy.sh root@IP            # ставит Docker, копирует код, базу и .env, поднимает agent + bot, проверяет /health и /ask
SKIP_BOT=1 scripts/deploy.sh root@IP # только API, если бот крутится у участника 1 (один токен — один процесс)
```

Повторный запуск — обновление. Код лежит в `/opt/uzex`, логи — `/opt/uzex/logs`.

## Структура

```
app/config.py     env и константы (DATA_START=03.01.2022, DATA_END=30.10.2026)
app/normalize.py  ТН ВЭД → товар, сорт, класс, name_key, регионы
app/db.py         read-only DuckDB, только SELECT/WITH, таймаут 10 с, внешний доступ отключён
app/resolve.py    поиск товаров/компаний/брокеров/регионов (rapidfuzz, RU/UZ, транслитерация)
app/lang.py       ru | uz | uz-cyrl
app/guard.py      мгновенные no_data: даты вне диапазона, темы вне реестра, несуществующие ИНН/сделки/контракты/брокеры
app/prompts.py    системный промпт: схема, правила, примеры
app/agent.py      цикл LLM ↔ инструменты, бюджет 70 с, ≤ 8 вызовов, запасные модели, автоподстройка параметров
app/verify.py     проверка чисел/ИНН/кодов/дат ответа по результатам инструментов
app/api.py        FastAPI, LRU-кэш 500, CORS, логи
app/static/       веб-интерфейс
scripts/build_db_fallback.py   сборка базы из CSV по схеме ТЗ (+ --check)
eval/             вопросы (questions.jsonl — 46 от участника 2, questions_gen.jsonl — 59 наших), прогон, отчёт
bot/              Telegram-бот (участник 1), Dockerfile
```
