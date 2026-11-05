"""Read-only DuckDB и безопасный run_sql."""
from __future__ import annotations

import datetime as dt
import decimal
import re
import threading
import time

import duckdb

from . import config

_local = threading.local()


def conn() -> duckdb.DuckDBPyConnection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = duckdb.connect(config.DB_PATH, read_only=True,
                           config={"enable_external_access": False, "threads": 4, "lock_configuration": True})
        _local.conn = c
    return c


class SQLRejected(ValueError):
    pass


_FORBIDDEN_WORDS = re.compile(
    r"\b(insert|update|delete|create|drop|alter|copy|attach|detach|install|load|pragma|export|import|set|call|"
    r"truncate|replace|merge|grant|revoke|vacuum|checkpoint|use|reset)\b",
    re.I,
)
_FORBIDDEN_FUNCS = re.compile(
    r"\b(read_csv\w*|read_parquet|parquet_scan|read_json\w*|read_text|read_blob|glob|getenv|"
    r"sniff_csv|duckdb_\w+|pragma_\w+|http\w*|current_setting|query_table|query)\s*\(",
    re.I,
)


def _strip_literals(sql: str) -> str:
    """Убрать строковые литералы и комментарии — ключевые слова ищем только в коде."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return sql


def validate_sql(sql: str) -> str:
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise SQLRejected("Пустой запрос.")
    code = _strip_literals(s)
    if ";" in code:
        raise SQLRejected("Разрешён только один запрос (без ';').")
    if not re.match(r"^\s*\(?\s*(select|with)\b", code, re.I):
        raise SQLRejected("Разрешены только SELECT / WITH.")
    m = _FORBIDDEN_WORDS.search(code)
    if m:
        # REPLACE как функция replace(...) — разрешаем
        if not (m.group(1).lower() == "replace" and re.search(r"\breplace\s*\(", code, re.I)
                and not re.search(r"\b(or\s+replace|replace\s+into)\b", code, re.I)):
            raise SQLRejected(f"Запрещённое слово: {m.group(1).upper()}.")
        other = [w for w in _FORBIDDEN_WORDS.findall(code) if w.lower() != "replace"]
        if other:
            raise SQLRejected(f"Запрещённое слово: {other[0].upper()}.")
    if re.search(r"\b(from|join)\s+''", code, re.I):
        raise SQLRejected("Чтение файлов запрещено — используй таблицы базы.")
    m = _FORBIDDEN_FUNCS.search(code)
    if m:
        raise SQLRejected(f"Запрещённая функция: {m.group(1)}.")
    return s


def _cell(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, dt.datetime):
        return v.strftime("%d.%m.%Y %H:%M:%S")
    if isinstance(v, dt.date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, (dt.time, dt.timedelta)):
        return str(v)
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        if v.is_integer() and abs(v) < 1e15:
            return int(v)
        return round(v, 4)
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple)):
        return [_cell(x) for x in v]
    return str(v)


def run_sql(sql: str, max_rows: int = config.SQL_MAX_ROWS, timeout: float = config.SQL_TIMEOUT_S) -> dict:
    """Выполнить SELECT. Возвращает {columns, rows, row_count, truncated, elapsed_ms} или бросает исключение."""
    s = validate_sql(sql)
    c = conn()
    timer = threading.Timer(timeout, c.interrupt)
    t0 = time.time()
    timer.start()
    try:
        cur = c.execute(s)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        row_count = len(rows)
        if truncated:
            row_count = c.execute(f"SELECT count(*) FROM ({s}) AS _q").fetchone()[0]
    except duckdb.InterruptException as e:
        raise TimeoutError(f"Запрос дольше {timeout:.0f} с — упрости его.") from e
    finally:
        timer.cancel()
    return {
        "columns": cols,
        "rows": [[_cell(v) for v in r] for r in rows],
        "row_count": int(row_count),
        "truncated": truncated,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def scalar(sql: str, params: list | None = None):
    r = conn().execute(sql, params or []).fetchone()
    return r[0] if r else None
