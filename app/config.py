"""Конфигурация: переменные окружения и константы данных."""
from __future__ import annotations

import datetime as dt
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(path: str) -> None:
    """Минимальный парсер .env (без зависимостей). Не перезаписывает уже заданные переменные."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] in "\"'" and v[:1] and v.count(v[0]) >= 2:
                v = v[1: v.index(v[0], 1)]
            else:
                v = v.split(" #", 1)[0].strip()  # комментарий в конце строки
            os.environ.setdefault(k.strip(), v)


_load_dotenv(os.path.join(ROOT, ".env"))

DB_PATH = os.getenv("DB_PATH", os.path.join(ROOT, "data", "uzex.duckdb"))
LOG_PATH = os.getenv("LOG_PATH", os.path.join(ROOT, "logs", "qa.jsonl"))

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-5.5")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))  # на один вызов; при таймауте — повтор
# запасные модели на случай 429 / ошибок провайдера (у OpenRouter лимит RPM — на каждую модель отдельно)
_DEFAULT_FALLBACK = ("anthropic/claude-sonnet-5,anthropic/claude-opus-5.5" if "openrouter" in LLM_BASE_URL
                     else "claude-opus-5-5" if "anthropic.com" in LLM_BASE_URL else "")
LLM_FALLBACK_MODELS = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", _DEFAULT_FALLBACK).split(",") if m.strip()]
LLM_RPM = int(os.getenv("LLM_RPM", "0"))  # свой лимит запросов/мин на модель; 0 = без ограничения (OpenRouter new account: 18)

# кэширование промпта: auto = включено для моделей Claude через OpenRouter; on / off — принудительно
LLM_PROMPT_CACHE = os.getenv("LLM_PROMPT_CACHE", "auto").lower()


def is_openrouter() -> bool:
    return "openrouter" in LLM_BASE_URL


def prompt_cache_for(model: str) -> bool:
    if LLM_PROMPT_CACHE == "off":
        return False
    if LLM_PROMPT_CACHE == "on":
        return True
    return is_openrouter() and ("claude" in model or model.startswith("anthropic/"))


PORT = int(os.getenv("PORT", "8000"))

DATA_START = dt.date(2022, 1, 3)
DATA_END = dt.date(2026, 10, 30)
DATA_START_STR = "03.01.2022"
DATA_END_STR = "30.10.2026"
MIN_YEAR, MAX_YEAR = 2022, 2026

TIME_BUDGET_S = float(os.getenv("TIME_BUDGET_S", "70"))
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "8"))
MAX_VERIFY_RETRIES = 2
SQL_TIMEOUT_S = 10.0
SQL_MAX_ROWS = 50
CACHE_SIZE = 500
