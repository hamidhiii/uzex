"""LOCAL TEST MOCK ONLY — never use these illustrative values in production.

Run with: uvicorn mock:app --host 127.0.0.1 --port 8000
The real AI/SQL service must replace this mock before the selection demo.
"""

import asyncio
import time
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="UZEX bot local mock (test only)")


class Question(BaseModel):
    question: str
    user_id: Optional[str] = None


def detect_lang(text: str) -> str:
    lower = text.lower()
    if any(ch in lower for ch in "ўқғҳ") or any(word in lower for word in ("қайси", "нечта", "бўйича")):
        return "uz-cyrl"
    if any(ch in lower for ch in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"):
        uz_words = ("қайси", "нечта", "йилда", "бўйича")
        return "uz-cyrl" if any(word in lower for word in uz_words) else "ru"
    uz_words = ("qaysi", "nechta", "bo‘yicha", "bo'yicha", "yilda", "bitim", "brokeri", "viloyat")
    return "uz" if any(word in lower for word in uz_words) else "ru"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/ask")
async def ask(q: Question):
    started = time.perf_counter()
    await asyncio.sleep(2)
    lang = detect_lang(q.question)
    if "2021" in q.question:
        if lang == "ru":
            answer = "Сделок за 2021 год в реестре нет: данные начинаются 03.01.2022."
        elif lang == "uz-cyrl":
            answer = "2021 йил учун реестрда битимлар йўқ: маълумотлар 03.01.2022 санасидан бошланади."
        else:
            answer = "2021-yil uchun reyestrda bitimlar yo‘q: ma’lumotlar 03.01.2022 sanasidan boshlanadi."
        return {
            "answer": answer,
            "status": "no_data",
            "lang": lang,
            "verified": True,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "evidence": [],
        }

    # These figures are deliberately illustrative test data from the supplied
    # mock specification. They are NOT facts from the UZEX registry.
    if lang == "ru":
        answer = "В тестовой заглушке за 2025 год по **карбамиду** указано **1 234** сделки на **56,7 млрд сум**. Это демонстрационные, не реальные данные."
        title = "Тестовый пример: число сделок и сумма по карбамиду за 2025"
    elif lang == "uz-cyrl":
        answer = "Тест заглушкасида 2025 йилда **карбамид** бўйича **1 234** та битим ва **56,7 млрд сўм** кўрсатилган. Бу ҳақиқий эмас, намойиш маълумотлари."
        title = "Тест мисоли: 2025 йилда карбамид битимлари ва сумма"
    else:
        answer = "Test zaglushkasida 2025-yilda **karbamid** bo‘yicha **1 234** ta bitim va **56,7 mlrd so‘m** ko‘rsatilgan. Bu haqiqiy emas, namoyish ma’lumotlari."
        title = "Test misoli: 2025-yilda karbamid bitimlari va summa"

    return {
        "answer": answer,
        "status": "ok",
        "lang": lang,
        "verified": True,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "evidence": [{
            "title": title,
            "sql": "SELECT COUNT(*) AS deals, ROUND(SUM(amount_uzs)) AS sum_uzs FROM deals WHERE product='Карбамид' AND year=2025",
            "row_count": 1,
            "columns": ["deals", "sum_uzs"],
            "rows": [[1234, 56712345678]],
        }],
    }
