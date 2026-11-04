"""ARES CODE — FastAPI: Mini App, HUD, SSE-чат (CHAT/CODE/SHELL), архивы, холодное хранение.

Один процесс: API + бот (polling) + фоновые задачи (уборщик). Авторизация Mini App —
стандартная проверка подписи Telegram initData (HMAC с BOT_TOKEN).
"""
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from core import agent, cleaner, coldstore, config, db, executor, resources, storage
from core import bot as botmod
from core.budget import budget, CHAT_SYSTEM

log = logging.getLogger("ares.api")

_HIST: dict = {}  # uid -> messages (CHAT-режим, в памяти процесса)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    tasks = [asyncio_create(cleaner.loop(notify=botmod.notify_owners))]
    if config.BOT_TOKEN and config.BOT_POLLING:
        tasks.append(asyncio_create(botmod.start_polling()))
    log.info("ARES CODE запущен: %s", budget.hud())
    yield
    for t in tasks:
        t.cancel()


def asyncio_create(coro):
    import asyncio
    return asyncio.get_event_loop().create_task(coro)


app = FastAPI(title="ARES CODE", lifespan=lifespan)


# ---------------------------------------------------------------- авторизация

def verify_initdata(init: str) -> dict:
    """Официальная проверка подписи Telegram WebApp initData."""
    if not init or not config.BOT_TOKEN:
        raise HTTPException(403, "нет initData/BOT_TOKEN")
    pairs = urllib.parse.parse_qsl(init, keep_blank_values=True)
    d = dict(pairs)
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(h, d.get("hash", "")):
        raise HTTPException(403, "подпись initData неверна")
    try:
        user = json.loads(d.get("user", "{}"))
    except json.JSONDecodeError:
        user = {}
    return {"id": int(user.get("id", 0) or 0), "name": user.get("first_name", "user")}


def auth(request: Request) -> dict:
    if config.ALLOW_UNAUTH_DEV:
        return {"id": 0, "name": "dev", "owner": True, "approved": True}
    u = verify_initdata(request.headers.get("x-telegram-init-data", ""))
    db.upsert_user(u["id"], u["name"])
    owner = db.is_owner(u["id"])
    return {"id": u["id"], "name": u["name"], "owner": owner,
            "approved": owner or db.is_approved(u["id"])}


def owner_only(user: dict):
    if not user.get("owner"):
        raise HTTPException(403, "только владелец")


def sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def safe_project(name: str | None) -> str:
    n = re.sub(r"[^A-Za-z0-9_-]", "", name or "")[:64]
    return n or "default"


# ---------------------------------------------------------------- страницы и статус

@app.get("/")
async def index():
    return FileResponse(str(config.ROOT / "webapp" / "index.html"))


@app.get("/healthz")
async def healthz():
    """Пинг для cron-job.org (каждые 10 минут)."""
    s = resources.snapshot()
    return {
        "ok": True,
        "name": "ARES CODE",
        "uptime_s": s["uptime_s"],
        "disk_pct": s["disk_pct"],
        "budget": {"hud": budget.hud(), "state": budget.state()},
        "providers": await agent.health(),
    }


@app.get("/api/hud")
async def api_hud(user: dict = Depends(auth)):
    return {"sys": resources.snapshot(),
            "budget": {"hud": budget.hud(), "state": budget.state()},
            "project": db.kv_get("active_project", "default")}


@app.post("/api/whoami")
async def api_whoami(user: dict = Depends(auth)):
    u = db.get_user(user["id"])
    return {"id": user["id"], "name": user["name"], "owner": user["owner"],
            "approved": user["approved"], "requested": bool(u and u["requested_at"]),
            "project": db.kv_get("active_project", "default")}


@app.post("/api/access_request")
async def api_access_request(user: dict = Depends(auth)):
    if user["owner"] or user["approved"]:
        return {"ok": True, "already": True}
    db.mark_requested(user["id"])
    await botmod.notify_owners(f"🔒 Заявка доступа из Mini App: {user['name']} id={user['id']}")
    # продублируем с кнопками ✅/⛔ напрямую
    if botmod._bot is not None:
        kb = botmod.kb_approve(user["id"])
        for oid in config.OWNER_TELEGRAM_IDS:
            try:
                await botmod._bot.send_message(oid, f"🔒 Заявка доступа: {user['name']} id={user['id']}",
                                               reply_markup=kb)
            except Exception:
                pass
    return {"ok": True}

@app.post("/api/chat")
async def api_chat(request: Request, user: dict = Depends(auth)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "ждём JSON")
    text = (body.get("text") or "").strip()
    images = body.get("images") or []
    if not text and not images:
        raise HTTPException(400, "пустой запрос")
    mode = (body.get("mode") or "CHAT").upper()
    if mode not in ("CHAT", "CODE", "SHELL"):
        mode = "CHAT"
    power = (body.get("power") or "AUTO").upper()
    project = safe_project(body.get("project") or db.kv_get("active_project") or "default")
    db.kv_set("active_project", project)
    (config.WORKSPACE / project).mkdir(parents=True, exist_ok=True)
    level = budget.complexity(text, power)

    async def gen():
        if not user["approved"]:
            yield sse({"t": "err", "s": "ACCESS DENIED — доступ не выдан"})
            yield sse({"t": "end"})
            return
        if not budget.can_accept():
            yield sse({"t": "err", "s": f"ЛИМИТ СУТОК ИСЧЕРПАН (100%). {budget.hud()}. "
                                        f"Подними TOK_BUDGET_DAY/REQ_BUDGET_DAY в .env"})
            yield sse({"t": "end"})
            return
        try:
            if mode == "SHELL":
                res = await executor.run(text, project)
                if res["out"]:
                    yield sse({"t": "out", "s": res["out"]})
                if res["err"]:
                    yield sse({"t": "out", "s": res["err"]})
                yield sse({"t": "step", "kind": "shell", "ok": res["exit"] == 0,
                           "s": res["cmd"], "exit": res["exit"]})
            elif mode == "CODE":
                async for ev in agent.code_stream(text, project, level):
                    yield sse(ev)
            else:  # CHAT
                msgs = _HIST.setdefault(user["id"], [])
                if not msgs:
                    msgs.append({"role": "system", "content": CHAT_SYSTEM})
                msgs.append({"role": "user", "content": text})
                if budget.compaction_needed(msgs):
                    msgs[:] = budget.compact(msgs)
                    yield sse({"t": "note", "s": "контекст сжат (компакция)"})
                reply = ""
                async for ev in agent.chat_stream(msgs, mode="CHAT", level=level, images=images):
                    if ev["t"] == "delta":
                        reply += ev["s"]
                    yield sse(ev)
                msgs.append({"role": "assistant", "content": reply})
                if len(msgs) > 40:
                    msgs[:] = [msgs[0]] + msgs[-39:]
        except Exception as e:
            yield sse({"t": "err", "s": f"{type(e).__name__}: {e}"})
        yield sse({"t": "state", "hud": budget.hud(), "state": budget.state()})
        yield sse({"t": "end"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- архивы / холод / уборка

@app.get("/api/archives")
async def api_archives(user: dict = Depends(auth)):
    rows = [dict(r) for r in db.list_archives(50)]
    return {"archives": rows}


@app.post("/api/archive")
async def api_archive(request: Request, user: dict = Depends(auth)):
    owner_only(user)
    body = await request.json()
    try:
        row = storage.make_archive(safe_project(body.get("project")))
        return {"ok": True, "archive": row}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/cold")
async def api_cold(request: Request, user: dict = Depends(auth)):
    owner_only(user)
    body = await request.json()
    project = safe_project(body.get("project"))
    try:
        row = db.latest_local_archive(project)
        if not row:
            row = storage.make_archive(project)
        res = coldstore.upload_archive(row)
        return {"ok": bool(res.get("ok")), "result": res, "archive_id": row["id"]}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/coldstatus")
async def api_coldstatus(user: dict = Depends(auth)):
    owner_only(user)
    return {"mailboxes": coldstore.status()}


@app.post("/api/restore")
async def api_restore(request: Request, user: dict = Depends(auth)):
    owner_only(user)
    body = await request.json()
    try:
        arch_id = int(body.get("archive_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "archive_id должен быть числом")
    row = db.get_archive(arch_id)
    if not row:
        raise HTTPException(404, "архив не найден")
    try:
        from pathlib import Path
        if row["cold"] and not Path(row["path"]).exists():
            coldstore.pull_archive(arch_id)
        res = storage.restore_archive(row)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/cleanup")
async def api_cleanup(request: Request, user: dict = Depends(auth)):
    owner_only(user)
    body = await request.json()
    dry = bool(body.get("dry_run", True))
    res = await cleaner.cleanup(dry_run=dry)
    removed = cleaner.purge_trash() if not dry else []
    return {"ok": True, **res, "purged_from_trash": removed}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="info")
