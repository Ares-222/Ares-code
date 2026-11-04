#!/usr/bin/env python3
"""ARES CODE — самопроверка установки (без сети).

  python3 check_install.py          # локальные проверки
  python3 check_install.py --net    # + Telegram getMe, пинг GLM, IMAP-логины ящиков

Критичные проверки падают с exit 1; сетевые и «нет ключей» — только предупреждения.
"""
import asyncio
import json
import re
import sys
import shutil
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config  # noqa: E402

# --- изоляция: проверка не трогает боевые данные ---
_TEST = Path(__file__).resolve().parent / "data_check"
config.DB_PATH = _TEST / "ares_check.db"
config.WORKSPACE = _TEST / "workspace"
config.ARCHIVE_DIR = _TEST / "archives"
config.TRASH_DIR = config.WORKSPACE / ".trash"
for d in (config.WORKSPACE, config.ARCHIVE_DIR, config.TRASH_DIR):
    d.mkdir(parents=True, exist_ok=True)

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"  [OK]   {name}")
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  [FAIL] {name} — {type(e).__name__}: {e}")


def warn(name, fn):
    try:
        out = fn()
        if asyncio.iscoroutine(out):
            loop = asyncio.get_event_loop()
            loop.run_until_complete(out)
        print(f"  [OK]   {name}")
    except Exception as e:
        print(f"  [WARN] {name} — {e}")


def main():
    net = "--net" in sys.argv

    print("\n1. Зависимости")
    def _deps():
        import fastapi, uvicorn, aiogram, httpx, psutil, dotenv  # noqa
    check("python-пакеты (fastapi, uvicorn, aiogram, httpx, psutil, dotenv)", _deps)

    print("\n2. Конфигурация .env")
    def _bot_token():
        assert config.BOT_TOKEN, "BOT_TOKEN пуст"
        assert re.match(r"^\d+:[\w-]{20,}$", config.BOT_TOKEN), "BOT_TOKEN не похож на формат 123:abc"
    check("BOT_TOKEN задан и по формату", _bot_token)
    def _glm():
        assert config.GLM_API_KEY, "GLM_API_KEY пуст"
        assert config.GLM_API_BASE.startswith("http"), "GLM_API_BASE кривой"
    check("GLM провайдер настроен", _glm)
    print("  [i]    Владельцы OWNER_TELEGRAM_IDS:", sorted(config.OWNER_TELEGRAM_IDS) or "ПУСТО — бот не знает, кого считать владельцем")
    for k, v in (("GEMINI_API_KEY", config.GEMINI_API_KEY),
                 ("OPENROUTER_API_KEY", config.OPENROUTER_API_KEY),
                 ("OMNIROUTE_API_KEY", config.OMNIROUTE_API_KEY),
                 ("BASE_URL", config.BASE_URL)):
        print(f"  [i]    {k}: {'задан' if v else 'ПУСТО (резерв/настройка позже)'}")
    mbs = config.coldstore_mailboxes()
    print(f"  [i]    Холодное хранилище: {len(mbs)} ящик(ов) настроено")

    print("\n3. База данных")
    def _db():
        from core import db
        db.connect()
        db.upsert_user(777000, "check"); db.set_approved(777000, True)
        assert db.is_approved(777000) and not db.is_owner(777000)
        db.add_usage("glm", "glm-4.7-flash", 100, 50, "check", 10, True)
        t, n = db.day_totals()
        assert t >= 150 and n >= 1
        s1 = db.next_seq(); db.add_archive("checkproj", "/tmp/x.zip", "ab" * 32, 123)
        assert db.next_seq() == s1 + 1
        db.kv_set("k", "v"); assert db.kv_get("k") == "v"
    check("SQLite: пользователи, журнал, архивы, kv", _db)

    print("\n4. Песочница-исполнитель")
    def _ex_ok():
        from core import executor
        r = asyncio.get_event_loop().run_until_complete(executor.run("echo ares", "checkproj"))
        assert r["exit"] == 0 and "ares" in r["out"], r
    check("echo в песочнице", _ex_ok)
    def _ex_wl():
        from core import executor
        r = asyncio.get_event_loop().run_until_complete(executor.run("bash -c 'echo hi'", "checkproj"))
        assert r["exit"] == 126 and r.get("rejected"), r
    check("вне белого списка — отказ (bash)", _ex_wl)
    def _ex_env():
        (config.WORKSPACE / "checkproj" / ".env").write_text("SECRET=1")
        from core import executor
        r = asyncio.get_event_loop().run_until_complete(executor.run("cat .env", "checkproj"))
        assert r["exit"] == 126 and ".env" in r["err"], r
    check("доступ к .env закрыт", _ex_env)
    def _ex_to():
        from core import executor
        r = asyncio.get_event_loop().run_until_complete(
            executor.run('python3 -c "import time; time.sleep(5)"', "checkproj", timeout=1))
        assert r["exit"] == 124 and r.get("timeout"), r
    check("таймаут 1с убивает сон 5с (exit 124)", _ex_to)
    def _ex_tail():
        from core import executor
        r = asyncio.get_event_loop().run_until_complete(
            executor.run('python3 -c "print(*range(120), sep=chr(10))"', "checkproj"))
        lines = r["out"].splitlines()
        assert len(lines) <= config.EXEC_OUT_MAX_LINES + 1, len(lines)
        assert "119" in r["out"] and "0" not in [l for l in lines], "хвост неверный"
    check(f"stdout — хвост {config.EXEC_OUT_MAX_LINES} строк", _ex_tail)

    print("\n5. Токен-экономика")
    def _bd():
        from core.budget import Budget
        b = Budget()
        config.TOK_BUDGET_DAY, config.REQ_BUDGET_DAY = 1000, 100
        config.WARN_AT, config.ECONOMY_AT = 0.8, 0.9
        import core.db as db
        db.ex("DELETE FROM ledger")
        for _ in range(5):  # 5 × 170 = 850 → 85% → WARN
            db.add_usage("t", "m", 170, 0, "check", 1, True)
        assert b.state() == "WARN", b.state()
        db.add_usage("t", "m", 50, 0, "check", 1, True)  # 900 → 90% → ECONOMY
        assert b.state() == "ECONOMY", b.state()
        assert b.max_iters() == 1 and b.out_lines() == 10
        db.add_usage("t", "m", 200, 0, "check", 1, True)  # 1100 → 110% → BLOCKED
        assert b.state() == "BLOCKED", b.state()
        assert b.max_iters() == 0
        assert Budget().complexity("реализуй полный проект архитектуры") == "STRONG"
        assert Budget().complexity("привет", "FAST") == "LIGHT"
        assert Budget().complexity("привет", "MAX") == "STRONG"
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x" * 200000}]
        assert Budget().compaction_needed(msgs)
        c = Budget().compact(msgs, "план", {"a.py"})
        assert c[0]["role"] == "system" and len(c) <= 4
    check("WARN→ECONOMY→BLOCKED, лимиты итераций, сложность, компакция", _bd)

    print("\n6. Архивы и холодное хранение")
    def _st():
        from core import storage
        p = config.WORKSPACE / "arcproj"; p.mkdir(exist_ok=True)
        (p / "a.py").write_text("print('ares')")
        (p / "sub").mkdir(exist_ok=True); (p / "sub" / "b.txt").write_text("data")
        (p / ".env").write_text("SECRET=1")
        row = storage.make_archive("arcproj")
        import zipfile
        with zipfile.ZipFile(row["path"]) as z:
            names = z.namelist()
        assert "passport.json" in names and "a.py" in names and "sub/b.txt" in names
        assert ".env" not in names, "секрет утёк в архив!"
        assert hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest() == row["sha256"]
    check("zip + passport.json + sha256, .env исключён", _st)
    def _rot():
        from core import coldstore
        n = len(config.coldstore_mailboxes())
        if n < 2:
            print(f"      (настроено {n} ящиков — ротация проверяется на ≥2; в .env их 4)")
            return
        idx = {coldstore.pick_index(s, n) for s in range(1, n + 1)}
        assert idx == set(range(n)), idx
    check("ротация ящиков round-robin (seq % N)", _rot)

    print("\n7. Агент (парсер, thinking, vision)")
    def _ag():
        from core import agent
        a = agent.parse_action('Вот результат:\n```json\n{"action":"run","cmd":"ls"}\n```')
        assert a and a["cmd"] == "ls"
        a = agent.parse_action('{"action":"write","content":"{\\"x\\":1}"}')
        assert a and a["action"] == "write"
        assert agent.parse_action("нет тут json") is None
        assert agent.has_images([{"role": "user", "content":
            [{"type": "text", "text": "с"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}]}])
    check("parse_action терпит ```-заборы, vision-детект", _ag)

    print("\n8. API и Mini App")
    def _api():
        import api as apimod  # noqa
        paths = {r.path for r in apimod.app.routes}
        for need in ("/", "/healthz", "/api/chat", "/api/hud", "/api/whoami",
                     "/api/archive", "/api/cold", "/api/restore", "/api/cleanup"):
            assert need in paths, need
    check("маршруты FastAPI на месте", _api)
    def _sign():
        assert config.BOT_TOKEN, "нужен BOT_TOKEN"
        import hashlib, hmac as hm, urllib.parse
        user = json.dumps({"id": 555, "first_name": "Check"}, separators=(",", ":"))
        pairs = {"auth_date": "1700000000", "query_id": "AAF", "user": user}
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hm.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hm.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        from api import verify_initdata
        u = verify_initdata(urllib.parse.urlencode({**pairs, "hash": h}))
        assert u["id"] == 555, u
        bad = False
        try:
            verify_initdata(urllib.parse.urlencode({**pairs, "hash": "0" * 64}))
        except Exception:
            bad = True
        assert bad, "поддельная подпись прошла!"
    check("подпись Telegram initData: верная принимается, поддельная — нет", _sign)
    def _web():
        html = (Path(__file__).parent / "webapp" / "index.html").read_text()
        for token in ("#DC0F0C", "JetBrains Mono", "telegram-web-app.js", "/api/chat",
                      "SHELL", "ARES", "Open Ares Code" if False else "◆"):
            assert token in html, token
    check("webapp: палитра ARES, шрифт, SSE, режимы", _web)

    if net:
        print("\n9. Сетевые проверки (--net)")
        warn("Telegram getMe", _net_telegram)
        warn("GLM живой пинг", _net_glm)
        warn("IMAP-логины ящиков", _net_imap)

    fails = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 62)
    print(f"ИТОГО: {len(RESULTS) - len(fails)} OK / {len(fails)} FAIL")
    if fails:
        for n, _, e in fails:
            print(f"  ✖ {n}: {e}")
    print("=" * 62)
    sys.exit(1 if fails else 0)


async def _net_telegram():
    import httpx
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.get(f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe")
    j = r.json()
    assert j.get("ok"), j
    print(f"      бот: @{j['result']['username']}")


async def _net_glm():
    from core import agent
    h = await agent.health(force=True)
    print("      провайдеры:", json.dumps(h, ensure_ascii=False)[:200])


async def _net_imap():
    from core import coldstore
    for m in coldstore.status():
        print("      ", m)


if __name__ == "__main__":
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
    shutil.rmtree(_TEST, ignore_errors=True)
