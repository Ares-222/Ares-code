"""ARES CODE — токен-экономика.

Что делает:
  * ведёт реальный учёт usage от провайдеров (fallback-оценка len/4), всё в SQLite;
  * HUD: TOK x/день | REQ y/день;
  * уровни: OK -> WARN (80%) -> ECONOMY (90%: только LIGHT, без авто-итераций, ответ ≤10 строк)
            -> BLOCKED (100%: новые задачи отклоняются);
  * компакция контекста при превышении CTX_COMPACT_AT (сохраняем план, активные файлы,
    последние 4 хода) — только между шагами plan→run→fix, цикл никогда не рвётся;
  * анти-деградация: серия ошибок провайдера ослабляет пороги на 50%, чтобы сессии
    fixes не блокировались на ровном месте.
"""
import time

from core import db, config


def fmt(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def estimate_tokens(text: str) -> int:
    """Fallback-оценка: примерно 4 символа на токен."""
    if not text:
        return 0
    return max(1, len(str(text)) // 4)


ECONOMY_NOTE = (
    "\n\nРЕЖИМ ECONOMY: дневной лимит почти исчерпан. Отвечай максимально коротко "
    "(до 10 строк), авто-исправления отключены, не повторяй код целиком."
)

CHAT_SYSTEM = (
    "Ты — ARES CODE, личный ИИ-агент владельца, живущий в терминале Telegram Mini App. "
    "Стиль: коротко, технично, по делу, без воды и извинений. Отвечай на языке пользователя. "
    "Если просят код — давай рабочий код целиком."
)


class Budget:

    def __init__(self):
        self.consec_errors = 0

    # ---------- учёт ----------
    def record(self, provider: str, model: str, prompt: int | None, completion: int | None,
               kind: str = "chat", latency_ms: int = 0, ok: bool = True):
        db.add_usage(provider, model, prompt or 0, completion or 0, kind, latency_ms, ok)
        if ok:
            self.consec_errors = 0
        else:
            self.consec_errors += 1

    # ---------- состояние ----------
    def _thresholds(self):
        relax = 1.5 if self.consec_errors >= 3 else 1.0  # анти-деградация: пороги ослаблены на 50%
        return min(1.0, config.WARN_AT * relax), min(1.0, config.ECONOMY_AT * relax)

    def day(self):
        return db.day_totals()

    def state(self) -> str:
        tok, req = self.day()
        warn, econ = self._thresholds()
        r = max(tok / max(1, config.TOK_BUDGET_DAY), req / max(1, config.REQ_BUDGET_DAY))
        if r >= 1.0:
            return "BLOCKED"
        if r >= econ:
            return "ECONOMY"
        if r >= warn:
            return "WARN"
        return "OK"

    def can_accept(self) -> bool:
        return self.state() != "BLOCKED"

    def hud(self) -> str:
        tok, req = self.day()
        return f"TOK {fmt(tok)}/{fmt(config.TOK_BUDGET_DAY)} | REQ {req}/{config.REQ_BUDGET_DAY}"

    # ---------- сложность и лимиты ----------
    def complexity(self, text: str, mode: str = "AUTO") -> str:
        mode = (mode or "AUTO").upper()
        if mode == "FAST":
            return "LIGHT"
        if mode == "MAX":
            return "STRONG"
        t = (text or "").lower()
        heavy = any(k in t for k in (
            "рефактор", "архитектур", "полностью", "реализуй", "проект", "напиши программу",
            "отлад", "оптимиз", "refactor", "implement", "architecture", "build", "app ",
        ))
        return "STRONG" if (heavy or len(text) > 400) else "LIGHT"

    def max_iters(self, state: str | None = None, level: str = "LIGHT") -> int:
        """Число циклов run→fix в CODE-режиме."""
        state = state or self.state()
        if state == "BLOCKED":
            return 0
        if state == "ECONOMY":
            return 1  # авто-итерации отключены
        if state == "WARN":
            return 3
        return 3 if level == "LIGHT" else 6

    def out_lines(self) -> int:
        return 10 if self.state() == "ECONOMY" else config.EXEC_OUT_MAX_LINES

    # ---------- компакция ----------
    def msgs_tokens(self, messages) -> int:
        total = 0
        for m in messages:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, str):
                total += estimate_tokens(c)
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict):
                        total += estimate_tokens(p.get("text", ""))
        return total

    def compaction_needed(self, messages) -> bool:
        return self.msgs_tokens(messages) > config.CTX_COMPACT_AT

    def compact(self, messages, plan_text: str = "", active_files=()):
        """Сохраняем: системные сообщения, план, активные файлы, последние 4 хода."""
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        stubs = []
        if plan_text:
            stubs.append({"role": "system", "content": "[ПЛАН — актуален]\n" + plan_text})
        if active_files:
            stubs.append({"role": "system", "content": "[АКТИВНЫЕ ФАЙЛЫ] " + ", ".join(sorted(active_files))})
        return sys_msgs + stubs + rest[-8:]


# синглтон на процесс
budget = Budget()
