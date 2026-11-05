"""resolve_entity: поиск товаров, компаний, брокеров, регионов по справочникам."""
from __future__ import annotations

import re
import threading
from functools import lru_cache

from rapidfuzz import fuzz

from . import db
from .normalize import PRODUCTS, REGIONS, name_key, norm_text, translit

_REGION_ALIASES = {uz: al for uz, _, al in REGIONS}

_lock = threading.Lock()
_IDX: dict | None = None

# окончания для грубого стемминга (RU и UZ), от длинных к коротким
_ENDINGS = sorted(
    "ами ями ого его ому ему ых их ой ей ом ем ов ев ам ям ах ях ую юю ая яя ое ее ые ие ии а я ы и у ю е о ь й "
    "larning larni lardan larda larga lari lar ning dagi dan ni ga da ni ka".split(),
    key=len, reverse=True,
)


def _stem_word(w: str) -> str:
    if len(w) <= 4 or w.isdigit():
        return w
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 4:
            return w[: -len(e)]
    return w


def stem(s: str) -> str:
    return " ".join(_stem_word(w) for w in s.split())


def _variants(text: str) -> list[str]:
    n = norm_text(text)
    out = {n, stem(n)}
    t = norm_text(translit(n))
    out |= {t, stem(t)}
    return [x for x in out if x]


def _score(q_vars: list[str], alias_vars: list[str], short: bool) -> float:
    best = 0.0
    for q in q_vars:
        for a in alias_vars:
            if q == a:
                return 100.0
            if short:
                # короткие алиасы (un, tuz, ПП, ДТ) — только точное совпадение слова
                if a in q.split():
                    best = max(best, 85.0)
                continue
            ts = 0.0
            if len(q.split()) > 1 or len(a.split()) > 1:
                ts = fuzz.token_set_ratio(q, a)
                if set(q.split()) != set(a.split()):
                    ts = min(ts, 90.0)  # подмножество слов — не полное совпадение
            s = max(fuzz.ratio(q, a), ts)
            # запрос — одно слово, алиас длиннее: «цемент» ⊂ «цемент портландский»
            if q in a.split() and len(q) >= 4:
                s = max(s, 92.0)
            best = max(best, s)
    return best


def _build() -> dict:
    c = db.conn()
    deals_cnt = dict(c.execute("SELECT product, count(*) FROM deals GROUP BY 1").fetchall())
    prods = c.execute("SELECT product, hs_code, name_uz, synonyms, unit FROM products").fetchall()
    product_info = {p: {"product": p, "hs_code": hs, "unit": u, "deals_count": int(deals_cnt.get(p, 0))}
                    for p, hs, _, _, u in prods}
    aliases = []  # (variants, product, matched_on, short)

    def add(alias: str, product: str, matched_on: str):
        alias = (alias or "").strip()
        if not alias:
            return
        v = _variants(alias)
        short = len(norm_text(alias)) <= 3
        aliases.append((v, product, matched_on, short))

    for p, hs, uz, syn, _ in prods:
        add(p, p, "product")
        for a in (uz or "").split(";"):
            add(a, p, "name_uz")
        for a in (syn or "").split(";"):
            add(a, p, "synonyms")
    # справочник из кода дополняет базу: синонимы не зависят от того, чья сборка uzex.duckdb подключена
    for _, p, uz, syn in PRODUCTS:
        if p in product_info:
            for a in uz.split(";"):
                add(a, p, "name_uz")
            for a in syn.split(";"):
                add(a, p, "synonyms")
    for raw, p in c.execute("SELECT DISTINCT raw_name, product FROM product_names").fetchall():
        add(raw, p, "raw_name")

    parts = c.execute("SELECT inn, name, role, region_uz, activity FROM participants").fetchall()
    companies = [{"inn": i, "name": n, "role": r, "region_uz": ru, "activity": a, "key": name_key(n)}
                 for i, n, r, ru, a in parts]
    brokers = [{"broker_code": b, "name": n, "inn": i, "region_uz": ru, "region_ru": rr, "key": name_key(n)}
               for b, n, i, ru, rr in c.execute("SELECT broker_code, name, inn, region_uz, region_ru FROM brokers").fetchall()]
    regions = []
    for uz, ru, al in c.execute("SELECT region_uz, region_ru, aliases FROM regions").fetchall():
        names = [uz, ru] + [a for a in (al or "").split(";")] + [a for a in _REGION_ALIASES.get(uz, "").split(";")]
        regions.append({"region_uz": uz, "region_ru": ru,
                        "variants": [(v, a) for a in names if a.strip() for v in _variants(a)]})
    return {"product_info": product_info, "aliases": aliases, "companies": companies,
            "by_inn": {x["inn"]: x for x in companies}, "brokers": brokers,
            "broker_by_code": {b["broker_code"]: b for b in brokers},
            "broker_by_inn": {b["inn"]: b for b in brokers}, "regions": regions}


def idx() -> dict:
    global _IDX
    if _IDX is None:
        with _lock:
            if _IDX is None:
                _IDX = _build()
    return _IDX


# ------------------------------------------------------------------ product
def resolve_product(text: str) -> dict:
    I = idx()
    qv = _variants(text)
    best: dict[str, tuple[float, str]] = {}
    for av, prod, matched_on, short in I["aliases"]:
        s = _score(qv, av, short)
        if s > best.get(prod, (0, ""))[0]:
            best[prod] = (s, matched_on)
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:5]
    cands = [dict(I["product_info"][p], score=round(s, 1), matched_on=m) for p, (s, m) in ranked if s >= 60]
    found = bool(cands) and cands[0]["score"] >= 80
    if found:  # оставляем только близких к лучшему
        top = cands[0]["score"]
        cands = [c for c in cands if c["score"] >= top - 5]
    return {"found": found, "candidates": cands}


# ------------------------------------------------------------------ company
def resolve_company(text: str) -> dict:
    I = idx()
    m = re.search(r"(?<!\d)(\d{9})(?!\d)", text or "")
    if m:
        inn = m.group(1)
        p = I["by_inn"].get(inn)
        if p:
            return {"found": True, "candidates": [{k: v for k, v in p.items() if k != "key"} | {"score": 100}]}
        b = I["broker_by_inn"].get(inn)
        if b:
            return {"found": True, "candidates": [{k: v for k, v in b.items() if k != "key"} | {"score": 100, "role": "Брокер"}]}
        return {"found": False, "candidates": [], "note": f"ИНН {inn} нет в справочнике участников и брокеров"}
    q = name_key(text)
    if not q:
        return {"found": False, "candidates": []}
    scored = []
    for p in I["companies"]:
        k = p["key"]
        if not k:
            continue
        if k == q:
            s = 100.0
        else:
            s = max(fuzz.ratio(q, k), fuzz.token_set_ratio(q, k) * (0.97 if len(q.split()) < len(k.split()) else 1.0))
        if s >= 70:
            scored.append((s, p))
    scored.sort(key=lambda x: -x[0])
    cands = [{k: v for k, v in p.items() if k != "key"} | {"score": round(s, 1)} for s, p in scored[:5]]
    found = bool(cands) and cands[0]["score"] >= 85
    return {"found": found, "candidates": cands,
            **({"note": "Несколько близких кандидатов — уточни или назови всех"}
               if len(cands) > 1 and cands[1]["score"] >= cands[0]["score"] - 3 else {})}


# ------------------------------------------------------------------ broker
def resolve_broker(text: str) -> dict:
    I = idx()
    t = (text or "").strip()
    m = re.search(r"(?i)(?:bk|бк)\s*[-_№]?\s*0*(\d{1,3})\b", t) or re.fullmatch(r"\s*0*(\d{1,3})\s*", t)
    if m:
        code = f"BK{int(m.group(1)):03d}"
        b = I["broker_by_code"].get(code)
        if b:
            return {"found": True, "candidates": [{k: v for k, v in b.items() if k != "key"} | {"score": 100}]}
        return {"found": False, "candidates": [], "note": f"Брокера {code} нет в справочнике (есть BK001–BK120)"}
    q = name_key(t)
    q = re.sub(r"\b(broker|брокер)\b", "", q).strip() or q
    scored = []
    for b in I["brokers"]:
        k = re.sub(r"\bbroker\b", "", b["key"]).strip()
        s = max(fuzz.ratio(q, k), fuzz.token_set_ratio(q, b["key"]))
        if s >= 70:
            scored.append((s, b))
    scored.sort(key=lambda x: -x[0])
    cands = [{k: v for k, v in b.items() if k != "key"} | {"score": round(s, 1)} for s, b in scored[:5]]
    return {"found": bool(cands) and cands[0]["score"] >= 85, "candidates": cands}


# ------------------------------------------------------------------ region
def resolve_region(text: str) -> dict:
    I = idx()
    qv = _variants(text)
    # убрать слова «область», «viloyati» и т.п. из запроса для дополнительного варианта
    extra = []
    for q in qv:
        e = re.sub(r"\b(област\w*|обл|viloyat\w*|вилоят\w*|region|регион\w*|shahr\w*|город\w*|шаҳр\w*|респ\w*)\b", "", q).strip()
        if e and e != q:
            extra.append(e)
    scored = []
    for r in I["regions"]:
        best = 0.0
        for v, _ in r["variants"]:
            for q in qv:
                best = max(best, 100.0 if q == v else fuzz.ratio(q, v))
            for q in extra:  # без слова «область» — чуть ниже, чтобы полное название побеждало
                best = max(best, 0.95 * (100.0 if q == v else fuzz.ratio(q, v)))
        scored.append((best, r))
    scored.sort(key=lambda x: -x[0])
    cands = [{"region_uz": r["region_uz"], "region_ru": r["region_ru"], "score": round(s, 1)} for s, r in scored[:3] if s >= 60]
    found = bool(cands) and cands[0]["score"] >= 80
    if found:
        cands = [c for c in cands if c["score"] >= cands[0]["score"] - 3]
    return {"found": found, "candidates": cands}


@lru_cache(maxsize=2048)
def _resolve_cached(kind: str, text: str) -> dict:
    fn = {"product": resolve_product, "company": resolve_company, "broker": resolve_broker, "region": resolve_region}.get(kind)
    if fn is None:
        return {"found": False, "candidates": [], "error": f"kind должен быть product|company|broker|region, получено {kind!r}"}
    return fn(text)


def resolve_entity(kind: str, text: str) -> dict:
    import copy
    return copy.deepcopy(_resolve_cached((kind or "").strip().lower(), (text or "").strip()))
