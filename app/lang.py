"""Определение языка вопроса: ru | uz | uz-cyrl."""
from __future__ import annotations

import re

_UZ_CYRL_LETTERS = re.compile(r"[ўқғҳЎҚҒҲ]")
_UZ_CYRL_WORDS = re.compile(
    r"\b(қанча|неча|нечта|йил\w*|қайси|битим\w*|шартнома\w*|вилоят\w*|бўйича|ким|қачон|нима|ва|учун|"
    r"ассалому|алайкум|салом|раҳмат|рахмат|сотилган|сотиб|олинган|миқдори|суммаси|нархи|тонна|кўп|энг|ҳажми|бўлган|канча|кайси|буйича|компания\w*си)\b",
    re.I,
)
_UZ_LAT = re.compile(
    r"(o[‘’'ʻ`]|g[‘’'ʻ`])|\b(qancha|nechta|necha|qaysi|yil\w*|bitim\w*|shartnoma\w*|viloyat\w*|bo[‘’'ʻ`]?yicha|"
    r"kim|qachon|nima|uchun|sotilgan|sotib|eng|ko[‘’'ʻ`]p|hajmi|summasi|narxi|kompaniya\w*|ma[‘’'ʻ`]lumot|"
    r"salom|assalomu|alaykum|rahmat|brokeri?|xaridor\w*|sotuvchi\w*|holat\w*|bekor|to[‘’'ʻ`]lov|muddati|o[‘’'ʻ`]rtacha|jami|umumiy|"
    r"paxta|sement|bug[‘’'ʻ`]?doy|tonna|qilingan|bo[‘’'ʻ`]lgan|mavjud|haqida|qaysi|ichida|bilan|dan|da)\b"
    r"|\d{4}-yil",
    re.I,
)
_CYRL = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[a-zA-Z]")


def detect(text: str) -> str:
    t = text or ""
    if _UZ_CYRL_LETTERS.search(t):
        return "uz-cyrl"
    n_cyr = len(_CYRL.findall(t))
    n_lat = len(_LAT.findall(t))
    if n_cyr >= n_lat and n_cyr > 0:
        # узбекская кириллица без специальных букв
        words = _UZ_CYRL_WORDS.findall(t.lower())
        strong = [w for w in words if w not in ("ва", "ким", "нима", "тонна")]
        if strong or len(words) >= 2:
            return "uz-cyrl"
        return "ru"
    if n_lat > 0:
        # латиница без кириллицы — это узбекский (русский латиницей не пишут; языки бота: ru | uz | uz-cyrl).
        # «salom», «assalomu alaykum», «XUSIT POLIMER» -> uz
        return "uz"
    return "ru"
