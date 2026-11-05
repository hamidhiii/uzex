"""Прогон вопросов через агента и отчёт eval/report.md.

python eval/run_eval.py                       # все файлы eval/questions*.jsonl, агент напрямую
python eval/run_eval.py --url http://host:8000/ask
python eval/run_eval.py --only trap --limit 10 --workers 3
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.verify import extract  # noqa: E402


def ask_direct(q: str) -> dict:
    from app.agent import answer
    return answer(q)


def ask_url(url: str):
    import httpx

    def f(q: str) -> dict:
        return httpx.post(url, json={"question": q, "user_id": "eval"}, timeout=120).json()
    return f


def numbers(ans: str) -> list[tuple[float, int, float]]:
    out = []
    for _, v, dec, mult, pct in extract(ans).numbers:
        out.append((v, dec, mult))
    return out


def num_match(ans: str, target: float, tol: float) -> bool:
    for v, dec, mult in numbers(ans):
        val = v * mult
        if target == 0 and val == 0:
            return True
        if target != 0 and abs(val - abs(target)) / abs(target) <= max(tol, 1e-9):
            return True
        if mult != 1 and round(abs(target) / mult, dec) == round(v, dec):
            return True
        if mult == 1 and dec <= 2 and round(abs(target), dec) == round(v, dec) and abs(target) >= 1:
            return True
    return False


def norm(s: str) -> str:
    return re.sub(r"[\"«»'‘’ʻ`]", "", str(s)).lower().replace("ё", "е")


def grade(item: dict, r: dict) -> tuple[bool, bool]:
    """-> (верно, выдумка)"""
    exp = item["expected"]
    ans = r.get("answer", "")
    st = r.get("status")
    if exp["type"] == "no_data":
        ok = st == "no_data"
        fabricated = st == "ok"
        return ok, fabricated
    if st != "ok":
        return False, False
    if exp["type"] == "number":
        tol = float(exp.get("tolerance", 0.005))
        targets = [exp["value"]] + [a for a in item.get("alt_values", []) if isinstance(a, (int, float))]
        return any(num_match(ans, t, tol) for t in targets), False
    if exp["type"] == "text":
        targets = [exp["value"]] + [a for a in item.get("alt_values", []) if isinstance(a, str)]
        return any(norm(t) in norm(ans) for t in targets), False
    return False, False


def pct(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    k = min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))
    return xs[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--only", help="категория")
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(ROOT, "eval", "report.md"))
    ap.add_argument("--model", help="модель для этого прогона (напр. anthropic/claude-haiku-5.5 — дешёвая отладка)")
    ap.add_argument("--fallback", help="запасные модели через запятую ('' — без запасных)")
    a = ap.parse_args()

    files = a.files or sorted(glob.glob(os.path.join(ROOT, "eval", "questions*.jsonl")))
    items = []
    for f in files:
        for line in open(f, encoding="utf-8"):
            if line.strip():
                x = json.loads(line)
                x["_src"] = os.path.basename(f)
                items.append(x)
    if a.only:
        items = [x for x in items if x.get("category") == a.only]
    if a.ids:
        items = [x for x in items if x["id"] in a.ids]
    if a.limit:
        items = items[: a.limit]
    if a.model or a.fallback is not None:
        if a.url:
            sys.exit("--model/--fallback работают только без --url (агент вызывается напрямую)")
        from app import config
        if a.model:
            config.LLM_MODEL = a.model
        if a.fallback is not None:
            config.LLM_FALLBACK_MODELS = [m.strip() for m in a.fallback.split(",") if m.strip()]
    fn = ask_url(a.url) if a.url else ask_direct

    def run(x):
        t = time.time()
        try:
            r = fn(x["question"])
        except Exception as e:
            r = {"answer": f"EXC {e}", "status": "error", "verified": False, "evidence": []}
        r["_t"] = time.time() - t
        ok, fab = grade(x, r)
        mark = "✅" if ok else ("🚨" if fab else "❌")
        m = r.get("_meta") or {}
        extra = f" llm={m.get('llm_calls')} tools={m.get('tool_calls')} tok={m.get('tokens')} t={m.get('llm_times')} err={m.get('errors')}" if m and (r['_t'] > 20 or not ok) else ""
        print(f"{mark} {x['id']:>5} {r['_t']:5.1f}s [{r.get('status')}/{'v' if r.get('verified') else '-'}] {x['question'][:70]}{extra[:400]}", flush=True)
        return x, r, ok, fab

    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(run, items))
    total_t = time.time() - t0

    times = [r["_t"] for _, r, _, _ in res]
    metas = [r.get("_meta") or {} for _, r, _, _ in res]
    tok = sum(m.get("tokens") or 0 for m in metas)
    cached = sum(m.get("cached_tokens") or 0 for m in metas)
    cost = sum(m.get("cost_usd") or 0 for m in metas)
    llm_q = sum(1 for m in metas if m.get("llm_calls"))
    models = sorted({m.get("model") for m in metas if m.get("model")})
    n_ok = sum(ok for *_, ok, _ in res)
    traps = [(x, r, ok, fab) for x, r, ok, fab in res if x["expected"]["type"] == "no_data"]
    fabs = sum(fab for *_, fab in res)
    verified = sum(1 for _, r, _, _ in res if r.get("verified"))

    def grp(key):
        g = {}
        for x, r, ok, fab in res:
            k = x.get(key, "?")
            s = g.setdefault(k, [0, 0])
            s[0] += ok
            s[1] += 1
        return g

    L = [f"# Отчёт прогона ({time.strftime('%Y-%m-%d %H:%M')})", "",
         f"- Вопросов: **{len(res)}** ({', '.join(sorted({x['_src'] for x in items}))})",
         f"- Верно: **{n_ok}/{len(res)} = {100 * n_ok / max(1, len(res)):.1f}%**",
         f"- Ловушки (`no_data`): {sum(ok for *_, ok, _ in traps)}/{len(traps)} верно, **выдумок: {fabs}**",
         f"- Ответов с verified=true: {verified}/{len(res)}",
         f"- Время ответа: p50 **{pct(times, 50):.1f} с**, p95 **{pct(times, 95):.1f} с**, max {max(times or [0]):.1f} с"
         f" (общее {total_t:.0f} с, параллельно по {a.workers})",
         f"- Модель: {', '.join(models) or '—'}; вопросов с LLM: {llm_q}; токенов: {tok:,} (из кэша: {cached:,})".replace(",", " ")
         + (f"; стоимость: **${cost:.2f}** (${cost / max(1, llm_q):.3f} на вопрос)" if cost else ""), "",
         "## По категориям", "", "| Категория | Верно | Всего | % |", "|---|---|---|---|"]
    for k, (o, n) in sorted(grp("category").items()):
        L.append(f"| {k} | {o} | {n} | {100 * o / n:.0f}% |")
    L += ["", "## По языкам", "", "| Язык | Верно | Всего | % |", "|---|---|---|---|"]
    for k, (o, n) in sorted(grp("lang").items()):
        L.append(f"| {k} | {o} | {n} | {100 * o / n:.0f}% |")
    L += ["", "## Все вопросы", "", "| id | lang | Вопрос | Ожидание | Ответ | Статус | Итог | с |", "|---|---|---|---|---|---|---|---|"]
    for x, r, ok, fab in res:
        e = x["expected"]
        exp = e["type"] if e["type"] == "no_data" else f"{e['value']}" + (f" / alt {x['alt_values'][:2]}" if x.get("alt_values") else "")
        ans = (r.get("answer") or "").replace("\n", " ").replace("|", "\\|")[:220]
        L.append(f"| {x['id']} | {x.get('lang')} | {x['question'].replace('|', '/')} | {exp} | {ans} | "
                 f"{r.get('status')}{' ✔' if r.get('verified') else ''} | {'✅' if ok else ('🚨 выдумка' if fab else '❌')} | {r['_t']:.1f} |")
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n" + "\n".join(L[:9]))
    print(f"\nОтчёт: {a.out}")


if __name__ == "__main__":
    main()
