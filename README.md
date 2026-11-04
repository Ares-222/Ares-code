# ◆ ARES CODE

Личный AI-агент уровня Claude Code, живущий в Telegram: терминал в Mini App,
автономный режим CODE (план → код → запуск → чтение ошибок → фикс), failover
по бесплатным провайдерам, токен-экономика, песочница-исполнитель, холодное
хранение проектов в почтовых ящиках.

**Стек:** Python 3.11+ · FastAPI + uvicorn (один процесс) · aiogram 3 · SQLite · httpx · psutil

---

## Архитектура

```
Telegram ──polling──▶ bot.py ──┐
                               ├─▶ db.py (SQLite: доступы, архивы, журнал токенов)
Telegram Mini App ─HTTPS──▶ api.py (FastAPI)
                             │    ├─ /api/chat  SSE: CHAT / CODE / SHELL
                             │    ├─ /api/hud   HUD (CPU/RAM/DISK + TOK/REQ)
                             │    └─ /healthz   пинг для cron-job.org
                             │
                 core/agent.py ─ LLM failover:
                 GLM-4.7-Flash → Gemini Flash → OpenRouter :free → OmniRoute
                 (thinking on/off · reasoning_content скрыт · vision → glm-4.6v-flash)
                             │
                 core/executor.py ─ песочница: белый список, rlimit, таймаут,
                             чистое окружение без секретов, хвосты вывода
                             │
        core/budget.py (экономика) · core/cleaner.py (уборка) ·
        core/storage.py (zip+passport+sha256) · core/coldstore.py (4 ящика, round-robin)
```

## Быстрый старт

```bash
unzip ares_code.zip && cd ares_code
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # заполни ключи (см. таблицу ниже)
python3 check_install.py      # самопроверка (все пункты должны быть OK)
bash run.sh                   # = python3 api.py (API + бот + фон в одном процессе)
```

Проверка с сетью (бот, GLM, ящики): `python3 check_install.py --net`

**Полная пошаговая настройка «что куда нажимать» — в [SETUP.md](SETUP.md)**
(там же глоссарий терминов и раздел про бесплатный хостинг).

## Что заполнить в .env

| Переменная | Что это | Где взять |
|---|---|---|
| `BOT_TOKEN` | токен бота | @BotFather |
| `OWNER_TELEGRAM_IDS` | твой Telegram ID (узнать: `/id` у бота) | — |
| `GLM_API_KEY` | ключ z.ai (основная модель) | z.ai |
| `GEMINI_API_KEY` | резерв #2 (~250 req/сутки) | aistudio.google.com |
| `OPENROUTER_API_KEY` | резерв #3 (:free-пул) | openrouter.ai |
| `OMNIROUTE_API_KEY` | резерв #4 — после установки OmniRoute | дашборд OmniRoute → Endpoints |
| `BASE_URL` | **HTTPS**-адрес Mini App | твой домен/туннель |

Пароли 4 почтовых ящиков — `COLDSTORE_EMAIL_1..4_LOGIN/PASS` (пароли внешних
приложений mail.ru). **`.env` никогда не попадает в архивы и репозиторий.**

## Failover-цепочка (как работает)

Запрос идёт в GLM-4.7-Flash. Любая ошибка — 429/квота/таймаут/5xx — мгновенно
перекидывает на следующую ступень: **Gemini Flash → OpenRouter :free → OmniRoute**.
В терминале это видно: строка `» provider glm:glm-4.7-flash`. Порядок и модели
меняются в `.env` без правки кода.

**OmniRoute** (github.com/diegosouzapw/OmniRoute) — самостоятельный локальный
роутер, отдаёт единый OpenAI-совместимый endpoint `localhost:20128/v1` и сам
падает по уровням «подписка → ключи → дешёвые → бесплатные пулы» (50+ провайдеров):

```bash
npm install -g omniroute && omniroute   # дашборд http://localhost:20128
# дашборд → Endpoints → скопируй ключ → OMNIROUTE_API_KEY в .env
```

*Про free-claude-code (GitHub): это гайд-репозиторий для официального Claude Code
CLI, а не библиотека — встраивать нечего. Его идея (бесплатные провайдеры через
OpenAI-совместимые мосты) у нас уже реализована, а агрегация бесплатных пулов —
это ровно OmniRoute.*

**GLM-специфика:** режим `thinking` включается только для планирования/STRONG
(MAX), в остальных случаях `disabled` — быстрее и экономнее. Поле
`reasoning_content` в терминал не выводится: доступно по клику на чип `🧠 мышление`
(тусклым цветом). Сообщения с картинками уходят на `glm-4.6v-flash`.

## Три режима терминала

| Режим | Что делает |
|---|---|
| **CHAT** | вопросы с памятью контекста и компакцией |
| **CODE** | автономный цикл: `plan → run → write/read → читаю ошибки → фикс → done`; сам ограничивает себя экономикой дня |
| **SHELL** | прямые команды в песочнице: `python3 …`, `git …`, `ffmpeg …` |

Кнопка `⚡ AUTO/FAST/MAX` — сложность: FAST = всегда LIGHT (быстро), MAX = всегда
STRONG (thinking + до 6 циклов исправлений).

## Токен-экономика

- Счётчик реального usage (если провайдер не отдал — оценка `len/4`), всё в SQLite.
- HUD в шапке: `TOK 12.3k/1M | REQ 45/1000`.
- Пороги: **80% WARN** → предупреждение; **90% ECONOMY** → только LIGHT, авто-итерации
  отключены, ответы ≤10 строк; **100% BLOCKED** → новые задачи отклоняются.
- Компакция контекста при `CTX_COMPACT_AT` (30k): сохраняются план, активные файлы
  и последние 4 хода — цикл plan→run→fix никогда не рвётся посередине.
- Анти-деградация: 3+ ошибки подряд ослабляют пороги на 50%, чтобы починка не
  блокировалась на ровном месте.

## Песочница исполнителя

Белый список: `python3/pip/node/npm/git/curl/ffmpeg/zip/sqlite3` + базовые утилиты.
`rlimit`: память 512МБ, CPU, размер файлов; таймаут 60с с убийством группы процессов;
максимум 2 параллельных воркеров; чистое окружение **без токенов и ключей**;
только `workspace/<проект>`; `.env` и базы недоступны. Вывод — хвост (40 строк,
при ошибке 100), середину никогда не режем. Честное ограничение: скрипты на python
технически могут читать файлы того же unix-пользователя — на VPS запускай ARES
под отдельным пользователем (см. v2: bubblewrap).

## Холодное хранение (4 ящика mail.ru)

`📦 АРХИВ` — zip + passport.json (список файлов, размеры, sha256), реестр в SQLite.
`❄ ХОЛОД` — архив уходит письмом в пул ящиков. **Ротация round-robin:**
`ящик № = (seq - 1) % N` по монотонному номеру архива — квоты расходуются
равномерно, при сбое ящика попытка со следующего, соответствие хранится в реестре.
Больше лимита письма — части `part i/M`; при восстановлении части склеиваются и
сверяется sha256. `.env` в архивы не попадает никогда.

## Доступ (гейт)

Не одобренный пользователь видит чёрный экран «ARES CODE · ДОСТУП» и кнопку 🔒.
Владельцу приходят кнопки ✅/⛔; одобренные попадают в SQLite. Владельцы
(`OWNER_TELEGRAM_IDS`) ходят всегда. `/start` присылает кнопку **◆ Open Ares Code**
(та же кнопка живёт в меню бота).

## Уборка

Ежедневно в `CLEANUP_HOUR` (04:00) или при диске >80%: неактивные проекты
переезжают в `workspace/.trash`, записи старше 24ч удаляются. Активный проект,
`.env`, базы и служебные каталоги не трогаются никогда. Dry-run: `/cleanup`.

## Деплой (кратко)

1. VPS: python3.11+, `pip install -r requirements.txt`, заполнить `.env`.
2. HTTPS-домен для Mini App (обязателен для Telegram): Caddy/nginx или
   `cloudflared tunnel --url http://localhost:8000` → получишь HTTPS → в `BASE_URL`.
3. `bash run.sh` (или systemd-юнит: `ExecStart=/opt/ares/.venv/bin/python api.py`).
4. cron-job.org → GET `https://твой-домен/healthz` каждые 10 минут (keep-alive).

## Самопроверка и поддержка

```bash
python3 check_install.py --net   # всё зелёное = готово
# у бота: /status /limits /health /archives /archive /cold /coldstatus /restore /cleanup /allow /deny /id
```

## Дорожная карта v2

1. **Bubblewrap-изоляция исполнителя** — отдельные namespace/uid для процессов песочницы.
2. **Webhook-режим бота** вместо polling + Redis-очередь задач.
3. **Мультипроектный UI**: список проектов, вкладки, история задач CODE-режима.
4. **Голосовой ввод** (Telegram voice → Whisper → CHAT).
5. **Git-синхронизация проектов** (авто-commit после каждого done + push в приватный репо).
6. **Планировщик задач** (cron внутри ARES: «каждый день в 9:00 запусти тесты и отпишись в чат»).
7. **Экспорт сессий** в markdown-отчёт + метрики скорости/качества фиксов.
