"""ARES CODE — Telegram-бот (aiogram 3): гейт доступа, команды владельца, уведомления.

Доступ:
  * владелец (OWNER_TELEGRAM_IDS) — прямой доступ всегда;
  * остальные видят «⛔ ДОСТУП ЗАКРЫТ» + кнопку 🔒 «Запросить доступ»;
  * владельцу приходит заявка с кнопками ✅ / ⛔; одобренные вносятся в SQLite.
Команды владельца: /status /limits /id /archives /archive /cold /restore /cleanup /allow /deny /health
Общие: /help — справка по командам бота и SHELL.
"""
import logging
import time

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, WebAppInfo)

from core import config, db, resources, storage, coldstore, agent, cleaner
from core.budget import budget, fmt

log = logging.getLogger("ares.bot")
router = Router()
_bot: Bot | None = None

GATE_TEXT = ("⛔ ДОСТУП ЗАКРЫТ\n\n"
             "Это приватный терминал ARES CODE.\n"
             "Нажми 🔒 — владелец получит заявку.")


def kb_open():
    """Кнопка Mini App, приходит в ответ на /start (и во всех ответах бота)."""
    if config.BASE_URL:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◆ Open Ares Code", web_app=WebAppInfo(url=config.BASE_URL))
        ]])
    return None


def kb_request():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Запросить доступ", callback_data="req_access")
    ]])


def kb_approve(uid: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅", callback_data=f"allow:{uid}"),
        InlineKeyboardButton(text="⛔", callback_data=f"deny:{uid}"),
    ]])


# ---------------------------------------------------------------- старт и доступ

@router.message(CommandStart())
async def cmd_start(m: Message):
    db.upsert_user(m.from_user.id, m.from_user.full_name)
    if db.is_owner(m.from_user.id) or db.is_approved(m.from_user.id):
        await m.answer(
            f"ARES CODE онлайн.\nПроект: {db.kv_get('active_project', 'default')}\n{budget.hud()}",
            reply_markup=kb_open())
    else:
        await m.answer(GATE_TEXT, reply_markup=kb_request())


@router.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Твой Telegram ID: {m.from_user.id}\n"
                   f"Впиши его в OWNER_TELEGRAM_IDS (.env) и перезапусти ARES.")


HELP_TEXT = ("ARES CODE · справка\n\n"
             "Терминал (кнопка ⬛): просто опиши задачу — агент сам спланирует, напишет код, "
             "запустит, починит ошибки и отчитается. Вопросы — просто спрашивай.\n"
             "!команда — прямая shell в песочнице, например: !python3 main.py\n"
             "Плагины с GitHub: «поставь плагин https://github.com/...» — склонирует в plugins/, "
             "зависимости поставит, подключит; «удали плагин <имя>» — уберёт.\n\n"
             "Команды бота:\n"
             "/start — открыть терминал\n"
             "/status — статус: CPU/RAM/диск, провайдеры, бюджет\n"
             "/limits — суточные лимиты\n"
             "/health — пинг провайдеров\n"
             "/archives — список архивов + кнопки «📥 получить файл»\n"
             "/archive <проект> — заархивировать и прислать файл сюда\n"
             "/cold <проект> — архив в холодное хранилище\n"
             "/coldstatus — статус почтовых ящиков\n"
             "/restore <id> — восстановить архив\n"
             "/plugins — плагины активного проекта\n"
             "/cleanup — уборка (посмотреть); /cleanup yes — выполнить\n"
             "/wipe [проект] — очистить содержимое проекта, даже активного\n"
             "/files on|off — автосохранение файлов задач в чат\n"
             "/id — мой Telegram ID\n"
             "/help — эта справка\n\n"
             "Команды SHELL (префикс ! в терминале):\n"
             "python3 python pip node npm git curl ffmpeg zip unzip sqlite3\n"
             "ls cat head tail grep find echo printf mkdir touch cp mv rm pwd\n"
             "wc sed awk tar jq diff sort uniq stat du df chmod\n\n"
             "Ограничения: только внутри workspace/<проект>; .env и системные пути закрыты.\n"
             "Файлы, созданные агентом, дублируются документами прямо в этот чат.")


@router.message(Command("help"))
async def cmd_help(m: Message):
    db.upsert_user(m.from_user.id, m.from_user.full_name)
    if db.is_owner(m.from_user.id) or db.is_approved(m.from_user.id):
        await m.answer(HELP_TEXT, reply_markup=kb_open())
    else:
        await m.answer(GATE_TEXT, reply_markup=kb_request())


@router.callback_query(F.data == "req_access")
async def cb_request(cb: CallbackQuery):
    uid = cb.from_user.id
    db.upsert_user(uid, cb.from_user.full_name)
    db.mark_requested(uid)
    for oid in config.OWNER_TELEGRAM_IDS:
        try:
            await _bot.send_message(oid, f"🔒 Заявка доступа:\n"
                                         f"{cb.from_user.full_name} (@{cb.from_user.username}) id={uid}",
                                    reply_markup=kb_approve(uid))
        except Exception:
            pass
    await cb.message.edit_text("✔ Заявка отправлена владельцу. Жди ✅.")
    await cb.answer()


@router.callback_query(F.data.startswith("allow:"))
async def cb_allow(cb: CallbackQuery):
    if not db.is_owner(cb.from_user.id):
        await cb.answer("только владелец", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    db.set_approved(uid, True)
    try:
        await _bot.send_message(uid, "✅ Доступ открыт. Жми ⬛ и работай.", reply_markup=kb_open())
    except Exception:
        pass
    await cb.message.edit_text(f"✔ Доступ выдан: id={uid}")
    await cb.answer()


@router.callback_query(F.data.startswith("deny:"))
async def cb_deny(cb: CallbackQuery):
    if not db.is_owner(cb.from_user.id):
        await cb.answer("только владелец", show_alert=True)
        return
    uid = int(cb.data.split(":", 1)[1])
    db.set_approved(uid, False)
    try:
        await _bot.send_message(uid, "⛔ Владелец отклонил заявку доступа.")
    except Exception:
        pass
    await cb.message.edit_text(f"✖ Отклонено: id={uid}")
    await cb.answer()


@router.message(Command("allow"))
async def cmd_allow(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    try:
        uid = int((m.text or "").split()[1])
    except (IndexError, ValueError):
        await m.answer("Формат: /allow <telegram_id>")
        return
    db.set_approved(uid, True)
    try:
        await _bot.send_message(uid, "✅ Доступ открыт. Жми ⬛ и работай.", reply_markup=kb_open())
    except Exception:
        pass
    await m.answer(f"✔ Доступ выдан: id={uid}")


@router.message(Command("deny"))
async def cmd_deny(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    try:
        uid = int((m.text or "").split()[1])
    except (IndexError, ValueError):
        await m.answer("Формат: /deny <telegram_id>")
        return
    db.set_approved(uid, False)
    await m.answer(f"✖ Доступ закрыт: id={uid}")


# ---------------------------------------------------------------- команды владельца

@router.message(Command("status"))
async def cmd_status(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    s = resources.snapshot()
    provs = await agent.health()
    ptxt = " ".join(f"{k}:{'✅' if v.get('ok') else '⚠️'}" for k, v in provs.items()) or "нет ключей"
    up = fmt_uptime(s["uptime_s"])
    arch = len(db.list_archives(200))
    await m.answer(
        f"ARES CODE · статус\n"
        f"{budget.hud()}  [{budget.state()}]\n"
        f"CPU {s['cpu']}% · RAM {s['ram_pct']}% · DISK {s['disk_pct']}% · up {up}\n"
        f"провайдеры: {ptxt}\n"
        f"проект: {db.kv_get('active_project', 'default')} · архивов: {arch}",
        reply_markup=kb_open())


@router.message(Command("limits"))
async def cmd_limits(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    warn, econ = budget._thresholds()
    await m.answer(
        f"Лимиты суток (правятся в .env):\n"
        f"TOK_BUDGET_DAY={config.TOK_BUDGET_DAY} · REQ_BUDGET_DAY={config.REQ_BUDGET_DAY}\n"
        f"WARN_AT={config.WARN_AT} · ECONOMY_AT={config.ECONOMY_AT}\n"
        f"CTX_COMPACT_AT={config.CTX_COMPACT_AT}\n"
        f"Сейчас: {budget.hud()} [{budget.state()}] (пороги: warn {warn:.2f} / econ {econ:.2f})")


@router.message(Command("health"))
async def cmd_health(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    h = await agent.health(force=True)
    lines = [f"{k}: {'OK ' + str(v.get('ms', '?')) + 'ms' if v.get('ok') else 'FAIL ' + v.get('err', '')}"
             for k, v in h.items()]
    await m.answer("Провайдеры:\n" + ("\n".join(lines) or "нет ключей"))


@router.message(Command("archives"))
async def cmd_archives(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    rows = db.list_archives(10)
    if not rows:
        await m.answer("Архивов пока нет. /archive <проект>")
        return
    lines = [f"#{r['id']} {r['project']} {fmt(r['size'])}Б {'❄' if r['cold'] else '💾'} "
             f"sha{r['sha256'][:8]}" for r in rows]
    btns = [[InlineKeyboardButton(text=f"📥 #{r['id']} {r['project']}",
                                  callback_data=f"getarc:{r['id']}")] for r in rows]
    await m.answer("Архивы (новые сверху) — жми, чтобы получить файл:\n" + "\n".join(lines),
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


@router.callback_query(F.data.startswith("getarc:"))
async def cb_getarc(cb: CallbackQuery):
    """Кнопка «📥 получить файл» у архива — файл приходит документом в этот чат."""
    if not db.is_owner(cb.from_user.id):
        await cb.answer("только владелец", show_alert=True)
        return
    try:
        aid = int((cb.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await cb.answer("битая кнопка", show_alert=True)
        return
    row = db.get_archive(aid)
    if not row:
        await cb.answer("архив не найден", show_alert=True)
        return
    await cb.answer("отправляю файл…")
    from pathlib import Path
    if row["cold"] and not Path(row["path"]).exists():
        try:
            coldstore.pull_archive(aid)
        except Exception as e:
            try:
                await _bot.send_message(cb.from_user.id, f"❄ не удалось достать #{aid} из холодного хранилища: {e}")
            except Exception:
                pass
            return
    ok = await send_archive_to_chat(cb.from_user.id, dict(row))
    if not ok:
        try:
            await _bot.send_message(
                cb.from_user.id,
                f"⚠️ Файл архива #{aid} не найден на диске. Пересоздай: /archive {row['project']}")
        except Exception:
            pass


@router.message(Command("archive"))
async def cmd_archive(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer("Формат: /archive <проект>")
        return
    try:
        row = storage.make_archive(parts[1])
        sent = await send_archive_to_chat(m.from_user.id, row)
        await m.answer(f"📦 {row['path']}\n{fmt(row['size'])}Б · sha256 {row['sha256'][:16]}…\n"
                       + ("Файл отправил в чат ⬆\n" if sent else "")
                       + f"В холодное хранилище: /cold {row['project']}")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@router.message(Command("cold"))
async def cmd_cold(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer("Формат: /cold <проект>")
        return
    project = parts[1]
    row = db.latest_local_archive(project)
    if not row:
        try:
            row = storage.make_archive(project)
        except Exception as e:
            await m.answer(f"Ошибка: {e}")
            return
    try:
        res = coldstore.upload_archive(row)
        if res.get("ok"):
            await m.answer(f"❄ Ушло в холодное хранилище: ящик {res['mailbox']}, "
                           f"частей: {res['parts']}\nтема: {res['subject']}")
        else:
            await m.answer(f"❌ {res.get('error')}")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@router.message(Command("restore"))
async def cmd_restore(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await m.answer("Формат: /restore <id архива> (см. /archives)")
        return
    row = db.get_archive(int(parts[1]))
    if not row:
        await m.answer("Архив не найден")
        return
    try:
        from pathlib import Path
        if row["cold"] and not Path(row["path"]).exists():
            await m.answer("❄ Тяну из холодного хранилища…")
            coldstore.pull_archive(row["id"])
        res = storage.restore_archive(row)
        await m.answer(f"✔ Восстановлено в workspace/{res['restored_to']} ({res['files']} файлов)")
    except Exception as e:
        await m.answer(f"Ошибка: {e}")


@router.message(Command("cleanup"))
async def cmd_cleanup(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    args = (m.text or "").split()
    dry = len(args) < 2 or args[1] != "yes"
    res = await cleaner.cleanup(dry_run=dry)
    if dry:
        w = res["would_move"]
        await m.answer("🧹 Dry-run — убралось бы:\n" + ("\n".join(w) if w else "(ничего)") +
                       "\nРеально: /cleanup yes")
    else:
        removed = cleaner.purge_trash()
        await m.answer("🧹 Убрано в .trash: " + (", ".join(res["moved"]) or "—") +
                       "\nУдалено из .trash: " + (", ".join(removed) or "—"))


@router.message(Command("wipe"))
async def cmd_wipe(m: Message):
    """Очистить содержимое проекта (в т.ч. активного) — всё в .trash."""
    if not db.is_owner(m.from_user.id):
        return
    parts = (m.text or "").split()
    project = "".join(c for c in (parts[1] if len(parts) > 1 else
                                   db.kv_get("active_project", "default"))
                       if c.isalnum() or c in "-") or "default"
    moved = cleaner.wipe_project(project)
    lst = (", ".join(moved[:10]) + ("…" if len(moved) > 10 else "")) if moved else "—"
    await m.answer(f"🧹 workspace/{project}: в .trash убрано ({len(moved)}):\n{lst}\n"
                   f"Из .trash удалится само через 24ч (или сразу: /cleanup yes)",
                   reply_markup=kb_open())


@router.message(Command("files"))
async def cmd_files(m: Message):
    """Вкл/выкл автосохранение файлов задач в чат."""
    if not db.is_owner(m.from_user.id):
        return
    parts = (m.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    cur = db.kv_get("send_files", "1") != "0"
    if arg in ("off", "выкл", "0"):
        db.kv_set("send_files", "0")
        await m.answer("📦 Файлы задач в чат: ВЫКЛ\nВернуть: /files on")
    elif arg in ("on", "вкл", "1"):
        db.kv_set("send_files", "1")
        await m.answer("📦 Файлы задач в чат: ВКЛ")
    else:
        await m.answer(f"📦 Файлы задач в чат: {'ВКЛ' if cur else 'ВЫКЛ'}\n"
                       f"Переключить: /files on | /files off")


@router.message(Command("coldstatus"))
async def cmd_coldstatus(m: Message):
    if not db.is_owner(m.from_user.id):
        return
    st = coldstore.status()
    if not st:
        await m.answer("Ящики не настроены (COLDSTORE_EMAIL_*_LOGIN/PASS в .env)")
        return
    lines = [f"{'✅' if x['ok'] else '⚠️'} {x['login']}"
             + (f" · архивов: {x['archives']}" if x["ok"] else f" · {x.get('err', '')}")
             for x in st]
    await m.answer("Холодное хранилище:\n" + "\n".join(lines))


@router.message(Command("plugins"))
async def cmd_plugins(m: Message):
    """Список плагинов/софта, установленных агентом в plugins/ активного проекта."""
    if not db.is_owner(m.from_user.id):
        return
    from pathlib import Path
    project = db.kv_get("active_project", "default")
    base = Path(config.WORKSPACE) / project / "plugins"
    if not base.exists() or not any(p.is_dir() for p in base.iterdir()):
        await m.answer(f"Плагинов в workspace/{project}/plugins нет.\n"
                       f"Ставь в терминале: «поставь плагин https://github.com/...» — "
                       f"агент склонирует, поставит зависимости и подключит.", reply_markup=kb_open())
        return
    lines = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        n = sum(1 for x in d.rglob("*") if x.is_file())
        lines.append(f"• {d.name} · {n} файлов")
    await m.answer(f"Плагины workspace/{project}/plugins:\n" + "\n".join(lines) +
                   "\nУдаление: в терминале «удали плагин <имя>» или /wipe",
                   reply_markup=kb_open())


# ---------------------------------------------------------------- прочее

@router.message()
async def fallback(m: Message):
    uid = m.from_user.id if m.from_user else 0
    db.upsert_user(uid, m.from_user.full_name if m.from_user else "user")
    if not (db.is_owner(uid) or db.is_approved(uid)):
        await m.answer(GATE_TEXT, reply_markup=kb_request())
        return
    txt = (m.text or "").strip()
    if txt.startswith("/"):
        # команда не сработала выше — честно говорим, какая именно, а не пугаем заглушкой
        cmd = txt.split()[0].split("@")[0]
        await m.answer(f"❓ Команда {cmd} не распознана.\n"
                       f"Весь список: /help\n"
                       f"Если нужна shell-команда — с префиксом ! в терминале ⬛.",
                       reply_markup=kb_open())
    else:
        await m.answer("Чат — для команд и файлов от агента. Задачи и вопросы — в терминале ⬛.",
                       reply_markup=kb_open())


def fmt_uptime(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    mm, _ = divmod(rem, 60)
    return f"{h}ч{mm}м" if h else f"{mm}м"


# ---------------------------------------------------------------- lifecycle

async def notify_owners(text: str):
    if _bot is None:
        return
    for oid in config.OWNER_TELEGRAM_IDS:
        try:
            await _bot.send_message(oid, text)
        except Exception:
            pass


async def send_archive_to_chat(uid: int, row: dict) -> bool:
    """Файл архива уходит документом прямо в чат Telegram."""
    if _bot is None:
        return False
    from pathlib import Path
    from aiogram.types import FSInputFile
    p = Path(row.get("path", ""))
    if not p.is_file():
        return False
    try:
        await _bot.send_document(
            uid, FSInputFile(str(p), filename=p.name),
            caption=f"📦 Архив #{row['id']} · {row['project']} · {fmt(row['size'])}Б · "
                    f"sha{row['sha256'][:8]}\nВ холодное хранилище: /cold {row['project']}")
        return True
    except Exception as e:
        log.warning("send_archive: %s", e)
        return False


async def start_polling():
    """Запускается задачей из api.py (один процесс)."""
    global _bot
    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN пуст — бот не запущен (работает только API)")
        return
    _bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await _bot.delete_webhook(drop_pending_updates=True)
    # WebApp-кнопка в меню бота (рядом с полем ввода) — чтобы терминал был всегда под рукой
    if config.BASE_URL:
        try:
            from aiogram.types import MenuButtonWebApp
            await _bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="◆ Open Ares Code", web_app=WebAppInfo(url=config.BASE_URL)))
        except Exception as e:
            log.warning("menu button: %s", e)
    # список команд в меню бота (кнопка ≡ слева от поля ввода)
    try:
        from aiogram.types import BotCommand
        await _bot.set_my_commands([
            BotCommand(command="start", description="открыть терминал"),
            BotCommand(command="help", description="справка: команды и лимиты"),
            BotCommand(command="status", description="статус системы"),
            BotCommand(command="limits", description="суточные лимиты"),
            BotCommand(command="health", description="пинг провайдеров"),
            BotCommand(command="archives", description="архивы + получить файл"),
            BotCommand(command="plugins", description="плагины проекта"),
            BotCommand(command="cleanup", description="уборка workspace"),
            BotCommand(command="wipe", description="очистить проект"),
            BotCommand(command="files", description="файлы задач в чат вкл/выкл"),
            BotCommand(command="id", description="мой Telegram ID"),
        ])
    except Exception as e:
        log.warning("set_my_commands: %s", e)
    log.info("бот запущен (polling)")
    await dp.start_polling(_bot, handle_signals=False)
