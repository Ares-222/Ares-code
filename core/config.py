"""ARES CODE — конфигурация из .env (все значения читаются динамически, чтобы tests могли подменять)."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # dotenv не критичен для тестов
    pass


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _ids(name: str) -> set:
    out = set()
    for part in os.getenv(name, "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


ROOT = Path(__file__).resolve().parent.parent

# --- каталоги (создаются сразу) ---
DATA_DIR = ROOT / "data"
WORKSPACE = ROOT / "workspace"
ARCHIVE_DIR = DATA_DIR / "archives"
TRASH_DIR = WORKSPACE / ".trash"
for _d in (DATA_DIR, WORKSPACE, ARCHIVE_DIR, TRASH_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "ares.db"

# --- Telegram ---
BOT_TOKEN = _s("BOT_TOKEN")
OWNER_TELEGRAM_IDS = _ids("OWNER_TELEGRAM_IDS")
BOT_POLLING = _s("BOT_POLLING", "1") == "1"
BASE_URL = _s("BASE_URL").rstrip("/")

# --- LLM цепочка ---
GLM_API_KEY = _s("GLM_API_KEY")
GLM_API_BASE = _s("GLM_API_BASE", "https://api.z.ai/api/paas/v4").rstrip("/")
GLM_MODEL = _s("GLM_MODEL", "glm-4.7-flash")
GLM_VISION_MODEL = _s("GLM_VISION_MODEL", "glm-4.6v-flash")

GEMINI_API_KEY = _s("GEMINI_API_KEY")
GEMINI_MODEL = _s("GEMINI_MODEL", "gemini-2.0-flash")

OPENROUTER_API_KEY = _s("OPENROUTER_API_KEY")
OPENROUTER_MODEL = _s("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_FALLBACKS = [m.strip() for m in os.getenv("OPENROUTER_FALLBACKS", "").split(",") if m.strip()]

# OmniRoute (github.com/diegosouzapw/OmniRoute) — локальный роутер бесплатных пулов,
# единый OpenAI-совместимый endpoint. Ставится отдельно: npm install -g omniroute
OMNIROUTE_BASE_URL = _s("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
OMNIROUTE_API_KEY = _s("OMNIROUTE_API_KEY")
OMNIROUTE_MODEL = _s("OMNIROUTE_MODEL", "auto")

# --- холодное хранение ---
COLDSTORE_IMAP_HOST = _s("COLDSTORE_IMAP_HOST", "imap.mail.ru")
COLDSTORE_IMAP_PORT = _i("COLDSTORE_IMAP_PORT", 993)
COLDSTORE_SMTP_HOST = _s("COLDSTORE_SMTP_HOST", "smtp.mail.ru")
COLDSTORE_SMTP_PORT = _i("COLDSTORE_SMTP_PORT", 465)
COLDSTORE_MAX_MAIL_MB = _i("COLDSTORE_MAX_MAIL_MB", 20)


def coldstore_mailboxes():
    """Список настроенных ящиков [(login, password), ...] в порядке конфигурации."""
    out = []
    for i in range(1, 9):
        login = os.getenv(f"COLDSTORE_EMAIL_{i}_LOGIN", "").strip()
        pwd = os.getenv(f"COLDSTORE_EMAIL_{i}_PASS", "").strip()
        if login and pwd:
            out.append((login, pwd))
    return out


# --- токен-экономика ---
TOK_BUDGET_DAY = _i("TOK_BUDGET_DAY", 1_000_000)
REQ_BUDGET_DAY = _i("REQ_BUDGET_DAY", 1000)
WARN_AT = _f("WARN_AT", 0.8)
ECONOMY_AT = _f("ECONOMY_AT", 0.9)
CTX_COMPACT_AT = _i("CTX_COMPACT_AT", 30_000)

# --- исполнитель ---
EXEC_TIMEOUT = _i("EXEC_TIMEOUT", 60)
EXEC_MEM_MB = _i("EXEC_MEM_MB", 512)
EXEC_OUT_MAX_LINES = _i("EXEC_OUT_MAX_LINES", 40)
EXEC_WORKERS = _i("EXEC_WORKERS", 2)

# --- уборка / фон ---
CLEANUP_HOUR = _i("CLEANUP_HOUR", 4)
DISK_CLEAN_AT = _f("DISK_CLEAN_AT", 0.8)
TRASH_TTL_H = _i("TRASH_TTL_H", 24)
HUD_INTERVAL_S = _i("HUD_INTERVAL_S", 5)
TYPEWRITER_MS = _i("TYPEWRITER_MS", 15)

PORT = _i("PORT", 8000)
ALLOW_UNAUTH_DEV = _s("ALLOW_UNAUTH_DEV", "0") == "1"
