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
             "Команды бота:\n"
             "/start — открыть терминал\n"
             "/status — статус: CPU/RAM/диск, провайдеры, бюджет\n"
             "/limits — суточные лимиты\n"
             "/health — пинг провайдеров\n"
             "/archives — список архивов\n"
             "/archive <проект> — заархивировать проект\n"
             "/cold <проект> — архив в холодное хранилище\n"
             "/coldstatus — статус почтовых ящиков\n"
             "/restore <id> — восстановить архив\n"
             "/cleanup — уборка (посмотреть); /cleanup yes — выполнить\n"
             "/id — мой Telegram ID\n"
             "/help — эта справка\n\n"
             "Команды SHELL (режим SHELL в терминале):\n"
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
    await m.answer("Архивы (новые сверху):\n" + "\n".join(lines))


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
        await m.answer(f"📦 {row['path']}\n{fmt(row['size'])}Б · sha256 {row['sha256'][:16]}…\n"
                       f"В холодное хранилище: /cold {row['project']}")
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


# ---------------------------------------------------------------- прочее

@router.message()
async def fallback(m: Message):
    uid = m.from_user.id if m.from_user else 0
    db.upsert_user(uid, m.from_user.full_name if m.from_user else "user")
    if db.is_owner(uid) or db.is_approved(uid):
        await m.answer("Чат-команды тут не обрабатываются — работай в терминале ⬛.", reply_markup=kb_open())
    else:
        await m.answer(GATE_TEXT, reply_markup=kb_request())


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
            BotCommand(command="archives", description="список архивов"),
            BotCommand(command="cleanup", description="уборка workspace"),
            BotCommand(command="id", description="мой Telegram ID"),
        ])
    except Exception as e:
        log.warning("set_my_commands: %s", e)
    log.info("бот запущен (polling)")
    await dp.start_polling(_bot, handle_signals=False)
