"""FastAPI: POST /ask, GET /health, GET / (веб-интерфейс). Контракт с ботом — не менять."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from collections import OrderedDict

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import agent, config, db
from .lang import detect
from .resolve import idx

app = FastAPI(title="UZEX deals agent", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC = os.path.join(os.path.dirname(__file__), "static")
_log_lock = threading.Lock()
_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()


class AskIn(BaseModel):
    question: str
    user_id: str | None = None


def _norm_q(q: str) -> str:
    q = q.lower().replace("ё", "е")
    q = re.sub(r"[‘’'ʼ`ʻ]", "'", q)
    q = re.sub(r"[?!.,;:«»\"]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _log(rec: dict) -> None:
    os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
    with _log_lock, open(config.LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


@app.on_event("startup")
def _warm():
    idx()  # прогреть справочники для resolve_entity
    db.conn()
    threading.Thread(target=agent.warmup, daemon=True).start()  # узнать параметры модели до первого вопроса


@app.get("/health")
def health():
    try:
        n = db.scalar("SELECT count(*) FROM deals")
        return {"status": "ok", "deals": int(n), "model": config.LLM_MODEL}
    except Exception as e:
        return JSONResponse({"status": "error", "error": str(e)}, status_code=503)


@app.post("/ask")
async def ask(body: AskIn, request: Request):
    t0 = time.time()
    q = (body.question or "").strip()[:1000]
    lang = detect(q)
    if not q:
        return {"answer": "Пустой вопрос.", "status": "error", "lang": lang, "verified": False, "elapsed_ms": 0, "evidence": []}
    key = _norm_q(q)
    with _cache_lock:
        hit = _cache.get(key)
        if hit:
            _cache.move_to_end(key)
    if hit:
        res = dict(hit, elapsed_ms=int((time.time() - t0) * 1000), cached=True)
    else:
        try:
            res = await run_in_threadpool(agent.answer, q)
        except Exception as e:  # не пробрасываем наружу
            res = {"answer": agent.TEXT["error"][lang], "status": "error", "lang": lang, "verified": False,
                   "elapsed_ms": int((time.time() - t0) * 1000), "evidence": [], "_meta": {"errors": [str(e)]}}
        if res.get("status") in ("ok", "no_data"):
            with _cache_lock:
                _cache[key] = {k: v for k, v in res.items() if k != "_meta"}
                while len(_cache) > config.CACHE_SIZE:
                    _cache.popitem(last=False)
    meta = res.pop("_meta", {}) if isinstance(res, dict) else {}
    _log({"ts": dt.datetime.now().isoformat(timespec="seconds"), "user_id": body.user_id, "question": q,
          "lang": res.get("lang"), "status": res.get("status"), "verified": res.get("verified"),
          "elapsed_ms": res.get("elapsed_ms"), "cached": res.get("cached", False), "answer": res.get("answer"),
          "sql": [e.get("sql") for e in res.get("evidence", [])], "meta": meta,
          "client": request.client.host if request.client else None})
    return res


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api:app", host="0.0.0.0", port=config.PORT)
