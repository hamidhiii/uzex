"""Цикл LLM ↔ инструменты с проверкой ответа.

CLI:  python -m app.agent "вопрос"
"""
from __future__ import annotations

import json
import sys
import time
import traceback

import collections
import threading

from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, OpenAI, RateLimitError

from . import config, db, guard, verify
from .lang import detect
from .prompts import SYSTEM, user_message
from .resolve import resolve_entity

TOOLS = [
    {"type": "function", "function": {
        "name": "resolve_entity",
        "description": "Найти товар, компанию, брокера или регион в справочниках по словам пользователя (RU/UZ, любое написание). "
                       "Вызывай ПЕРЕД фильтром по сущности. Возвращает found и до 5 кандидатов.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["product", "company", "broker", "region"]},
            "text": {"type": "string", "description": "как написал пользователь: 'paxta', 'цемента', 'XUSIT POLIMER', '996831451', 'bk45', 'Самарканд'"},
        }, "required": ["kind", "text"]}}},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "Выполнить один SELECT/WITH к DuckDB (только чтение, ≤50 строк в ответе, таймаут 10 с). "
                       "Все вычисления (суммы, средние, доли, округления) делай здесь.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string"},
            "purpose": {"type": "string", "description": "что считаем, коротко, на языке вопроса"},
        }, "required": ["sql", "purpose"]}}},
    {"type": "function", "function": {
        "name": "submit_answer",
        "description": "Финальный ответ пользователю. Все числа — только из результатов инструментов.",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "no_data"]},
        }, "required": ["answer", "status"]}}},
]
SUBMIT_ONLY = [TOOLS[2]]

TEXT = {
    "safe": {
        "ru": "Не удалось надёжно сформулировать ответ — показываю результат запроса «{title}» как есть:",
        "uz": "Javobni ishonchli shakllantirib bo‘lmadi — «{title}» so‘rovi natijasi:",
        "uz-cyrl": "Жавобни ишончли шакллантириб бўлмади — «{title}» сўрови натижаси:",
    },
    "nothing": {
        "ru": "**Не удалось получить ответ по данным.** Попробуйте переформулировать вопрос (товар, период, регион, компания).",
        "uz": "**Ma’lumotlar bo‘yicha javob olinmadi.** Savolni boshqacha yozib ko‘ring (tovar, davr, viloyat, kompaniya).",
        "uz-cyrl": "**Маълумотлар бўйича жавоб олинмади.** Саволни бошқача ёзиб кўринг (товар, давр, вилоят, компания).",
    },
    "error": {
        "ru": "Произошла техническая ошибка, ответ не получен. Попробуйте ещё раз.",
        "uz": "Texnik xatolik yuz berdi, javob olinmadi. Qayta urinib ko‘ring.",
        "uz-cyrl": "Техник хатолик юз берди, жавоб олинмади. Қайта уриниб кўринг.",
    },
}

_client: OpenAI | None = None
_UNSUPPORTED: dict[str, set] = {}  # модель -> параметры, которые она не принимает


def _with_cache(messages: list) -> list:
    """Кэширование промпта (Anthropic через OpenRouter): системный промпт со схемой (~6 тыс. токенов) помечается
    cache_control — повторные вызовы читают его из кэша за 10% цены. Плюс точка кэша на последнем сообщении,
    чтобы растущая история диалога тоже кэшировалась между шагами агента."""
    out = []
    last = len(messages) - 1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and isinstance(m.get("content"), str) and m["content"] and (
                m.get("role") == "system" or (i == last and m.get("role") in ("user", "tool"))):
            m = dict(m, content=[{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}])
        out.append(m)
    return out


class RateLimiter:
    """Скользящее окно 60 с на модель: не отправлять больше LLM_RPM запросов в минуту."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self.calls: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.cooldown: dict[str, float] = {}  # модель -> время, до которого после 429 её не трогаем
        self.lock = threading.Lock()

    def wait_time(self, model: str) -> float:
        now = time.time()
        cool = max(0.0, self.cooldown.get(model, 0.0) - now)
        if self.rpm <= 0:
            return cool
        with self.lock:
            q = self.calls[model]
            while q and now - q[0] > 60:
                q.popleft()
            return max(cool, 0.0 if len(q) < self.rpm else 60 - (now - q[0]) + 0.05)

    def take(self, model: str, max_wait: float) -> bool:
        if self.rpm <= 0:
            w = self.wait_time(model)
            if w > max_wait:
                return False
            if w > 0:
                time.sleep(w)
            return True
        while True:
            w = self.wait_time(model)
            if w <= 0:
                with self.lock:
                    q = self.calls[model]
                    if len(q) < self.rpm:
                        q.append(time.time())
                        return True
                continue
            if w > max_wait:
                return False
            time.sleep(min(w, 1.0))
            max_wait -= min(w, 1.0)

    def penalize(self, model: str, seconds: float = 15.0):
        """Получили 429 — модель на паузу (следующая попытка уйдёт в запасную), окно считаем заполненным."""
        with self.lock:
            self.cooldown[model] = max(self.cooldown.get(model, 0.0), time.time() + seconds)
        if self.rpm <= 0:
            return
        with self.lock:
            q = self.calls[model]
            now = time.time()
            while len(q) < self.rpm:
                q.append(now)


LIMITER = RateLimiter(config.LLM_RPM)


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY or "none",
                         timeout=config.LLM_TIMEOUT, max_retries=0)
    return _client


def _fmt_num(v) -> str:
    if isinstance(v, bool) or v is None:
        return "" if v is None else str(v)
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    if isinstance(v, float):
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
        return s[:-3] if s.endswith(",00") else s
    return str(v)


def table_md(ev: dict, n: int = 10) -> str:
    cols = ev["columns"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in ev["rows"][:n]:
        lines.append("| " + " | ".join(_fmt_num(x) for x in r) + " |")
    return "\n".join(lines)


class Agent:
    def __init__(self, question: str, lang: str | None = None):
        self.q = question.strip()
        self.lang = lang or detect(self.q)
        self.t0 = time.time()
        self.evidence: list[dict] = []
        self.allowed = verify.Allowed()
        self.allowed.add_question(self.q)
        for d in (config.DATA_START_STR, config.DATA_END_STR, "01.01.2022", "31.10.2026"):
            self.allowed.add_value(d)
        # границы периодов (первый/последний день месяца) — это трактовка вопроса, а не данные
        import calendar
        for y in range(config.MIN_YEAR, config.MAX_YEAR + 1):
            for m in range(1, 13):
                self.allowed.strings.add(f"01.{m:02d}.{y}")
                self.allowed.strings.add(f"{calendar.monthrange(y, m)[1]:02d}.{m:02d}.{y}")
        self.allowed.add_value([41, 14, 120, 500000, 6616, 14392])  # размеры справочников (известны из схемы)
        self.tool_calls = 0
        self.sql_calls = 0
        self.verify_fails = 0
        self.errors: list[str] = []
        self.llm_calls = 0
        self.text_replies = 0
        self.model_used = None
        self.llm_times: list[float] = []
        self.tokens = 0
        self.cached_tokens = 0
        self.cost = 0.0

    # ------------------------------------------------------------ helpers
    def elapsed(self) -> float:
        return time.time() - self.t0

    def result(self, answer: str, status: str, verified: bool) -> dict:
        return {"answer": answer, "status": status, "lang": self.lang, "verified": verified,
                "elapsed_ms": int(self.elapsed() * 1000), "evidence": self.evidence,
                "_meta": {"tool_calls": self.tool_calls, "llm_calls": self.llm_calls, "verify_fails": self.verify_fails,
                          "errors": self.errors, "tokens": self.tokens, "cached_tokens": self.cached_tokens,
                          "cost_usd": round(self.cost, 5), "model": self.model_used, "llm_times": self.llm_times}}

    def safe_answer(self, technical: bool = False) -> dict:
        """technical=True — сбой LLM/время вышло: без данных это error (не кэшируется), а не «данных нет»."""
        last = next((e for e in reversed(self.evidence) if e.get("kind") == "sql" and e["row_count"] > 0), None)
        if last is None:
            if technical:
                return self.result(TEXT["error"][self.lang], "error", False)
            return self.result(TEXT["nothing"][self.lang], "no_data", False)
        txt = TEXT["safe"][self.lang].format(title=last["title"]) + "\n\n" + table_md(last)
        return self.result(txt, "ok", False)

    # ------------------------------------------------------------ tools
    def tool_resolve(self, args: dict) -> dict:
        kind, text = args.get("kind", ""), args.get("text", "")
        r = resolve_entity(kind, text)
        self.allowed.add_value(r.get("candidates"))
        cands = r.get("candidates") or []
        cols = list(cands[0].keys()) if cands else ["found"]
        self.evidence.append({
            "kind": "resolve", "title": f"resolve_entity · {kind}: «{text}»",
            "sql": f"resolve_entity('{kind}', '{text}')", "row_count": len(cands),
            "columns": cols, "rows": [[c.get(k) for k in cols] for c in cands] if cands else [[False]],
        })
        return r

    def tool_sql(self, args: dict) -> dict:
        sql, purpose = args.get("sql", ""), args.get("purpose", "") or "Запрос"
        self.sql_calls += 1
        try:
            r = db.run_sql(sql)
        except Exception as e:  # ошибка — текстом обратно в LLM
            self.errors.append(f"sql: {e}")
            return {"error": f"{type(e).__name__}: {e}"}
        self.allowed.add_result(r)
        self.evidence.append({"kind": "sql", "title": purpose, "sql": sql.strip(), "row_count": r["row_count"],
                              "columns": r["columns"], "rows": r["rows"]})
        out = {k: r[k] for k in ("columns", "rows", "row_count", "truncated")}
        if r["row_count"] == 0:
            out["note"] = "0 строк — если это ответ на вопрос, то данных нет (status=no_data)."
        return out

    # ------------------------------------------------------------ LLM
    def _create(self, model: str, kw: dict):
        """Вызов с автоподстройкой: если модель не принимает параметр (tool_choice=required, temperature),
        убираем его и запоминаем для этой модели."""
        unsupported = _UNSUPPORTED.setdefault(model, set())
        for _ in range(4):
            args = dict(kw, model=model, tool_choice="auto" if "tool_choice" in unsupported else "required")
            if "temperature" in unsupported:
                args.pop("temperature", None)
            caching = config.prompt_cache_for(model) and "cache_control" not in unsupported
            if caching:
                args["messages"] = _with_cache(args["messages"])
            if config.is_openrouter():
                args["extra_body"] = {"usage": {"include": True}}  # вернуть стоимость запроса
            try:
                return client().chat.completions.create(**args)
            except BadRequestError as e:
                # OpenRouter кладёт текст ошибки провайдера в body.metadata
                text = str(e) + json.dumps(getattr(e, "body", None) or {}, default=str)
                used = {"temperature"} if "temperature" in args else set()
                if args["tool_choice"] == "required":
                    used.add("tool_choice")
                if caching:
                    used.add("cache_control")
                # параметр, который был в ЭТОМ запросе (другой поток мог уже пометить его — тогда просто повторяем)
                bad = next((p for p in ("tool_choice", "temperature", "cache_control") if p in text and p in used), None)
                if bad is None and caching:
                    bad = "cache_control"  # непонятная ошибка с разметкой кэша — повторить без кэша
                if bad is None:
                    raise
                unsupported.add(bad)
        return client().chat.completions.create(**args)

    def call_llm(self, messages: list, tools: list) -> object:
        models = [config.LLM_MODEL] + [m for m in config.LLM_FALLBACK_MODELS if m != config.LLM_MODEL]
        last_err: Exception | None = None
        for attempt in range(6):
            remaining = config.TIME_BUDGET_S - self.elapsed() - 3
            if remaining < 5:
                break
            # первая модель, у которой есть место в окне; иначе ждём основную
            model = next((m for m in models if LIMITER.wait_time(m) <= 0), None)
            if model is None:
                model = min(models, key=LIMITER.wait_time)
            if not LIMITER.take(model, max_wait=remaining - 5):
                break
            kw = dict(messages=messages, tools=tools, temperature=0, max_tokens=2000,
                      timeout=max(5.0, min(config.LLM_TIMEOUT, remaining)))
            self.llm_calls += 1
            t_call = time.time()
            try:
                resp = self._create(model, kw)
            except RateLimitError as e:
                LIMITER.penalize(model)
                self.errors.append(f"429 {model}")
                last_err = e
                continue
            except (APITimeoutError, APIConnectionError) as e:
                self.errors.append(f"{type(e).__name__} {model}")
                last_err = e
                if isinstance(e, APIConnectionError) and not isinstance(e, APITimeoutError):
                    # обрыв сети / DNS: мгновенный повтор бесполезен, даём сети 1–3 с
                    time.sleep(min(3.0, 1.0 + attempt, max(0.0, config.TIME_BUDGET_S - self.elapsed() - 10)))
                continue
            except APIStatusError as e:
                if isinstance(e, BadRequestError) or e.status_code < 500:
                    raise
                self.errors.append(f"{e.status_code} {model}")
                last_err = e
                continue
            u = getattr(resp, "usage", None)
            if u:
                self.tokens += u.total_tokens or 0
                details = getattr(u, "prompt_tokens_details", None)
                self.cached_tokens += (getattr(details, "cached_tokens", 0) or 0) if details else 0
                cost = getattr(u, "cost", None)
                if cost is None and getattr(u, "model_extra", None):
                    cost = u.model_extra.get("cost")
                if cost:
                    self.cost += float(cost)
            self.llm_times.append(round(time.time() - t_call, 1))
            self.model_used = model
            return resp.choices[0].message
        raise last_err or TimeoutError("LLM: бюджет времени исчерпан")

    # ------------------------------------------------------------ main
    def run(self) -> dict:
        pre = guard.precheck(self.q, self.lang)
        if pre:
            return self.result(pre["answer"], "no_data", True)

        messages: list = [{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": user_message(self.q, self.lang)}]
        while True:
            if self.elapsed() > config.TIME_BUDGET_S - 4:
                self.errors.append("time budget exceeded")
                return self.safe_answer(technical=True)
            tools = TOOLS if self.tool_calls < config.MAX_TOOL_CALLS else SUBMIT_ONLY
            try:
                msg = self.call_llm(messages, tools)
            except Exception as e:
                self.errors.append(f"llm: {type(e).__name__}: {e}")
                if self.evidence:
                    return self.safe_answer(technical=True)
                raise

            calls = msg.tool_calls or []
            if not calls:
                messages.append({"role": "assistant", "content": msg.content or ""})
                self.text_replies += 1
                if self.text_replies == 1:  # первый раз — попросить ответить через инструменты
                    messages.append({"role": "user", "content":
                                     "Отвечай только через инструменты: получи данные (resolve_entity / run_sql) "
                                     "и заверши вызовом submit_answer(answer, status)."})
                    continue
                # повторно текстом — считаем это ответом и проверяем как обычно
                outcome = self._submit({"answer": msg.content or "", "status": "ok"})
                if isinstance(outcome, dict) and "answer" in outcome and "status" in outcome and "evidence" in outcome:
                    return outcome
                messages.append({"role": "user", "content": outcome["feedback"] + " Ответь вызовом submit_answer."})
                continue

            messages.append({"role": "assistant", "content": msg.content or None, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls]})
            final = None
            for c in calls:
                name = c.function.name
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "submit_answer":
                    outcome = self._submit(args)
                    if "evidence" in outcome:
                        final = outcome
                        content = "OK"
                    else:
                        content = outcome["feedback"]
                else:
                    self.tool_calls += 1
                    if self.tool_calls > config.MAX_TOOL_CALLS:
                        res = {"error": "Лимит вызовов исчерпан. Вызови submit_answer по уже полученным данным."}
                    elif name == "resolve_entity":
                        res = self.tool_resolve(args)
                    elif name == "run_sql":
                        res = self.tool_sql(args)
                    else:
                        res = {"error": f"Нет инструмента {name}"}
                    content = json.dumps(res, ensure_ascii=False, default=str)
                    if self.tool_calls == config.MAX_TOOL_CALLS:
                        content += "\n[Это последний вызов инструмента — дальше только submit_answer.]"
                messages.append({"role": "tool", "tool_call_id": c.id, "content": content})
            if final:
                return final

    def _submit(self, args: dict) -> dict:
        answer = (args.get("answer") or "").strip()
        status = args.get("status") if args.get("status") in ("ok", "no_data") else "ok"
        if not answer:
            return {"feedback": "ОТКЛОНЕНО: пустой ответ."}
        vr = verify.check(answer, self.allowed)
        if status == "ok" and self.sql_calls == 0 and vr.numbers_checked > 0 and not vr.ok:
            feedback = "ОТКЛОНЕНО: в ответе есть числа, но run_sql не вызывался. Сначала получи данные через run_sql."
        elif vr.ok:
            return self.result(answer, status, True)
        else:
            feedback = ("ОТКЛОНЕНО проверкой: значения " + ", ".join(vr.unverified[:15]) +
                        " не найдены в результатах инструментов. Посчитай их через run_sql (ROUND в SQL) "
                        "или убери из ответа, затем снова вызови submit_answer.")
        self.verify_fails += 1
        self.errors.append(f"verify: {vr.unverified[:10]}")
        if self.verify_fails > config.MAX_VERIFY_RETRIES:
            return self.safe_answer()
        return {"feedback": feedback}


def warmup() -> None:
    """Один дешёвый вызов на старте: узнать, какие параметры модель не принимает (temperature, tool_choice)."""
    for model in [config.LLM_MODEL] + config.LLM_FALLBACK_MODELS:
        ag = Agent("warmup", "ru")
        try:
            ag._create(model, dict(messages=[{"role": "user", "content": "Вызови submit_answer с answer='ok'."}],
                                   tools=SUBMIT_ONLY, temperature=0, max_tokens=50, timeout=20))
        except Exception:
            pass


def answer(question: str, lang: str | None = None) -> dict:
    ag = Agent(question, lang)
    try:
        return ag.run()
    except Exception as e:
        ag.errors.append(f"fatal: {type(e).__name__}: {e}")
        ag.errors.append(traceback.format_exc()[-1500:])
        r = ag.result(TEXT["error"][ag.lang], "error", False)
        return r


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Сколько сделок было в 2025 году?"
    r = answer(q)
    print(r["answer"])
    print("\n---", r["status"], "| verified:", r["verified"], "| lang:", r["lang"], "|", r["elapsed_ms"], "ms |", r["_meta"])
    for e in r["evidence"]:
        print(f"\n[{e['title']}] rows={e['row_count']}\n{e['sql']}\n{e['columns']}\n{e['rows'][:5]}")
