"""Проверка ответа: каждое число, ИНН, код брокера и дата должны быть в результатах инструментов."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

MULT = [
    (r"трлн|trln|триллион\w*|trillion", 1e12),
    (r"млрд|mlrd|миллиард\w*|milliard\w*|mlrd\.", 1e9),
    (r"млн|mln|миллион\w*|million\w*|mln\.", 1e6),
    (r"тыс|ming|тысяч\w*|минг", 1e3),
]
_MULT_RE = re.compile(r"^\s*(" + "|".join(p for p, _ in MULT) + r")\.?(?![a-zа-я])", re.I)

_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_TIME_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_INN_RE = re.compile(r"(?<![\d.,])(?<!\d )(\d{9})(?![.,]\d)(?!\d)(?! \d{3}\b)")
_BK_RE = re.compile(r"\bBK\s*-?\s*(\d{3})\b", re.I)
# число: 1 234 567,89 | 1234567.89 | 12,5 | 1,234,567
_NUM_RE = re.compile(
    r"(?<![\w.,])-?(\d{1,3}(?:[    ]\d{3})+(?:[.,]\d+)?|\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?|\d+(?:[.,]\d+)?)(?![\d])"
)
_LIST_MARK = re.compile(r"^(\s*)(\d{1,2})[.)]\s", re.M)


def parse_num(tok: str) -> tuple[float, int]:
    """'1 234,56' -> (1234.56, 2 знака). '1,234,567' -> 1234567."""
    t = re.sub(r"[    ]", "", tok)
    if re.fullmatch(r"\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?", t):
        t = t.replace(",", "")
    t = t.replace(",", ".")
    dec = len(t.split(".")[1]) if "." in t else 0
    return float(t), dec


@dataclass
class Extracted:
    numbers: list = field(default_factory=list)  # (text, value, decimals, multiplier, is_pct)
    inns: list = field(default_factory=list)
    brokers: list = field(default_factory=list)
    dates: list = field(default_factory=list)  # 'DD.MM.YYYY'


def extract(text: str) -> Extracted:
    ex = Extracted()
    t = text or ""
    t = t.replace("**", " ").replace("`", " ")
    t = _LIST_MARK.sub(r"\1 ", t)  # «1. », «2) » — нумерация списков
    for m in _DATE_RE.finditer(t):
        ex.dates.append(f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{m.group(3)}")
    for m in _ISO_RE.finditer(t):
        ex.dates.append(f"{m.group(3)}.{m.group(2)}.{m.group(1)}")
    t = _DATE_RE.sub(" ", t)
    t = _ISO_RE.sub(" ", t)
    t = _TIME_RE.sub(" ", t)
    for m in _BK_RE.finditer(t):
        ex.brokers.append(f"BK{m.group(1)}")
    t = _BK_RE.sub(" ", t)
    for m in _INN_RE.finditer(t):
        ex.inns.append(m.group(1))
    t = _INN_RE.sub(" ", t)
    for m in _NUM_RE.finditer(t):
        tok = m.group(1)
        try:
            v, dec = parse_num(tok)
        except ValueError:
            continue
        rest = t[m.end():]
        mult = 1.0
        mm = _MULT_RE.match(rest)
        if mm:
            w = mm.group(1).lower()
            for p, k in MULT:
                if re.fullmatch(p, w, re.I) or re.match(p, w, re.I):
                    mult = k
                    break
        is_pct = bool(re.match(r"^\s*(%|процент|foiz|фоиз)", rest, re.I))
        ex.numbers.append((tok.strip(), v, dec, mult, is_pct))
    return ex


# ------------------------------------------------------------------ разрешённые значения
def _nums_in_text(s: str) -> list[float]:
    out = []
    s2 = _DATE_RE.sub(" ", s)
    for m in re.finditer(r"\d+(?:[.,]\d+)?", s2):
        try:
            out.append(float(m.group(0).replace(",", ".")))
        except ValueError:
            pass
    return out


class Allowed:
    def __init__(self):
        self.nums: set[float] = set()
        self.strings: set[str] = set()

    def add_value(self, v):
        if v is None:
            return
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            if math.isfinite(v):
                self.nums.add(float(v))
                self.nums.add(abs(float(v)))
                self.strings.add(str(int(v)) if float(v).is_integer() else str(v))
            return
        if isinstance(v, (list, tuple)):
            for x in v:
                self.add_value(x)
            return
        if isinstance(v, dict):
            for x in v.values():
                self.add_value(x)
            return
        s = str(v)
        self.strings.add(s)
        for d in _DATE_RE.finditer(s):
            self.strings.add(f"{int(d.group(1)):02d}.{int(d.group(2)):02d}.{d.group(3)}")
            for x in (d.group(1), d.group(2), d.group(3)):
                self.nums.add(float(x))
        for x in _nums_in_text(s):
            self.nums.add(x)
        if re.fullmatch(r"\d{9}", s):
            self.strings.add(s)
        for m in re.finditer(r"BK\d{3}", s):
            self.strings.add(m.group(0))

    def add_result(self, res: dict):
        self.add_value(res.get("rows"))
        rc = res.get("row_count")
        if rc is not None:
            self.nums.add(float(rc))

    def add_question(self, q: str):
        for _, v, _, mult, _ in extract(q).numbers:
            self.nums.add(v)
            self.nums.add(v * mult)
        ex = extract(q)
        self.strings.update(ex.inns)
        self.strings.update(ex.brokers)
        self.strings.update(ex.dates)
        for d in ex.dates:
            dd, mm, yy = d.split(".")
            self.nums.update({float(dd), float(mm), float(yy)})


def _close(shown: float, dec: int, mult: float, pct: bool, allowed: set[float]) -> bool:
    target = shown * mult
    for a in allowed:
        cands = [a]
        if pct:
            cands.append(a * 100)
        for c in cands:
            c = abs(c)
            if c == target:
                return True
            if target != 0 and abs(c - target) / max(abs(target), 1e-12) <= 0.005:
                return True
            # равно после округления до того же числа знаков (в единицах множителя)
            if mult != 1 or dec >= 0:
                if round(c / mult, dec) == round(shown, dec):
                    return True
    return False


@dataclass
class VerifyResult:
    ok: bool
    unverified: list = field(default_factory=list)
    numbers_checked: int = 0


def check(answer: str, allowed: Allowed) -> VerifyResult:
    ex = extract(answer)
    bad = []
    n = 0
    for inn in ex.inns:
        n += 1
        if inn not in allowed.strings:
            bad.append(inn)
    for bk in ex.brokers:
        n += 1
        if bk not in allowed.strings:
            bad.append(bk)
    for d in ex.dates:
        n += 1
        if d not in allowed.strings:
            bad.append(d)
    for tok, v, dec, mult, pct in ex.numbers:
        n += 1
        if mult == 1 and not pct and v.is_integer() and 2022 <= v <= 2026:
            continue  # годы из диапазона данных
        if not _close(abs(v), dec, mult, pct, allowed.nums):
            bad.append(tok + (" " + {1e3: "тыс", 1e6: "млн", 1e9: "млрд", 1e12: "трлн"}[mult] if mult != 1 else "")
                       + ("%" if pct else ""))
    # уникальные
    seen, uniq = set(), []
    for b in bad:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    return VerifyResult(ok=not uniq, unverified=uniq, numbers_checked=n)
