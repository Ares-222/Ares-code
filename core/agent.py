"""ARES CODE — LLM-агент.

Failover-цепочка: GLM-4.7-Flash -> Gemini Flash -> OpenRouter :free-пул.
* GLM: OpenAI-compatible endpoint из GLM_API_BASE, параметр thinking
  {"type": "disabled"|"enabled"} — disabled для LIGHT/CHAT и шагов-исполнителей,
  enabled для планирования/отладки (STRONG/MAX).
* reasoning_content из ответа НЕ печатается в основной терминал — уходит отдельным
  событием "reason", UI показывает его тускло по клику на 🧠.
* vision: если в сообщении есть изображение — GLM переключается на GLM_VISION_MODEL
  (glm-4.6v-flash), у Gemini/OpenRouter картинка идёт нативным форматом.
* Агент-режим (единый): сам решает — обычный текстовый ответ ИЛИ работа JSON-действиями
  plan→run/write/edit/read/list→fix; сырой JSON пользователю никогда не показывается
  (буферизуется, выполняется и отображается как чистые шаги ⏺ Bash/Write/Edit…).
"""
import asyncio
import json
import time

import httpx

from core import config, executor
from core.budget import budget, estimate_tokens, ECONOMY_NOTE, CHAT_SYSTEM

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# OmniRoute — локальный роутер бесплатных пулов (OpenAI-совместимый endpoint)


class LLMError(Exception):
    pass


class AllProvidersFailed(Exception):
    pass


# ---------------------------------------------------------------- утилиты

def _providers():
    """Failover-цепочка по порядку: GLM → Gemini → OpenRouter → OmniRoute."""
    chain = []
    if config.GLM_API_KEY:
        chain.append("glm")
    if config.GEMINI_API_KEY:
        chain.append("gemini")
    if config.OPENROUTER_API_KEY:
        chain.append("openrouter")
    if config.OMNIROUTE_API_KEY:
        chain.append("omniroute")
    return chain


def has_images(messages) -> bool:
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    return True
    return False


def attach_images(messages, images):
    """images: [{"mime": "image/jpeg", "data_b64": "..."}] — прикрепить к последнему user-сообщению."""
    if not images:
        return messages
    msgs = [dict(m) for m in messages]
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            c = msgs[i].get("content")
            parts = [{"type": "text", "text": c}] if isinstance(c, str) else list(c or [])
            for img in images:
                data = (img or {}).get("data_b64", "")
                if data:
                    mime = (img or {}).get("mime", "image/jpeg")
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
            msgs[i]["content"] = parts
            break
    return msgs


def parse_action(text: str):
    """Достаёт первый валидный JSON-объект с ключом action (терпит ```-заборы и болтовню вокруг)."""
    if not text:
        return None
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "action" in obj:
                return obj
    return None


# ---------------------------------------------------------------- стримы провайдеров

async def _stream_openai_compat(url, key, messages, model, display, extra_body=None, extra_headers=None):
    """Единый SSE-стрим для GLM (z.ai) и OpenRouter. Отдаёт события-кортежи."""
    if any(ord(ch) > 127 for ch in key):
        raise LLMError("API-ключ содержит не-ASCII символы — похоже, в .env ещё стоит заглушка. Вставь реальный ключ")
    yield ("provider", display)
    body = {"model": model, "messages": messages, "stream": True}
    if extra_body:
        body.update(extra_body)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as cli:
        async with cli.stream("POST", url, json=body, headers=headers) as r:
            if r.status_code >= 400:
                raw = (await r.aread()).decode("utf-8", "replace")
                raise LLMError(f"HTTP {r.status_code}: {raw[:280]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"])[:280])
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    rc = d.get("reasoning_content") or d.get("reasoning")
                    if rc:
                        yield ("reason", rc)  # в основной терминал не идёт
                    if d.get("content"):
                        yield ("delta", d["content"])
                u = chunk.get("usage")
                if u:
                    yield ("usage", (u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0))


def _to_gemini(messages):
    contents, system = [], []
    for m in messages:
        c = m.get("content")
        if m.get("role") == "system":
            if isinstance(c, str) and c:
                system.append(c)
            continue
        parts = []
        if isinstance(c, str):
            if c:
                parts.append({"text": c})
        elif isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text" and p.get("text"):
                    parts.append({"text": p["text"]})
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        head, b64 = url.split(",", 1)
                        mime = head[5:].split(";")[0] or "image/jpeg"
                        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        contents.append({"role": "model" if m.get("role") == "assistant" else "user",
                         "parts": parts or [{"text": ""}]})
    return contents, "\n".join(system)


async def _stream_gemini(messages, model):
    yield ("provider", f"gemini:{model}")
    contents, system = _to_gemini(messages)
    body = {"contents": contents}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = f"{GEMINI_URL}/{model}:streamGenerateContent?alt=sse&key={config.GEMINI_API_KEY}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as cli:
        async with cli.stream("POST", url, json=body) as r:
            if r.status_code >= 400:
                raw = (await r.aread()).decode("utf-8", "replace")
                raise LLMError(f"HTTP {r.status_code}: {raw[:280]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"])[:280])
                for cand in chunk.get("candidates") or []:
                    for p in ((cand.get("content") or {}).get("parts")) or []:
                        if p.get("text"):
                            yield ("delta", p["text"])
                u = chunk.get("usageMetadata")
                if u:
                    yield ("usage", (u.get("promptTokenCount") or 0, u.get("candidatesTokenCount") or 0))


async def _stream(name, messages, vision: bool, thinking: str):
    """Диспетчер по провайдерам с vision-роутингом."""
    if name == "glm":
        model = config.GLM_VISION_MODEL if vision else config.GLM_MODEL
        async for ev in _stream_openai_compat(
            f"{config.GLM_API_BASE}/chat/completions", config.GLM_API_KEY, messages, model,
            display=f"glm:{model}",
            extra_body={"thinking": {"type": thinking}},
        ):
            yield ev
    elif name == "gemini":
        async for ev in _stream_gemini(messages, config.GEMINI_MODEL):
            yield ev
    elif name == "openrouter":
        async for ev in _stream_openai_compat(
            OPENROUTER_URL, config.OPENROUTER_API_KEY, messages, config.OPENROUTER_MODEL,
            display=f"openrouter:{config.OPENROUTER_MODEL}",
            extra_headers={"HTTP-Referer": config.BASE_URL or "https://ares.local", "X-Title": "ARES CODE"},
        ):
            yield ev
    elif name == "omniroute":
        async for ev in _stream_openai_compat(
            f"{config.OMNIROUTE_BASE_URL}/chat/completions", config.OMNIROUTE_API_KEY,
            messages, config.OMNIROUTE_MODEL,
            display=f"omniroute:{config.OMNIROUTE_MODEL}",
        ):
            yield ev


async def llm_stream(messages, level: str = "LIGHT", purpose: str = "chat", images=None):
    """Failover по цепочке + один повторный проход против разовых сбоев/пустых потоков.

    События: ("provider",s) ("delta",s) ("reason",s) ("usage",(p,c)) ("fail",s)."""
    messages = attach_images(messages, images)
    vision = has_images(messages)
    thinking = "enabled" if (purpose == "plan" or level == "STRONG") else "disabled"
    chain = _providers()
    if not chain:
        yield ("fail", "Ни один провайдер не настроен: заполни GLM_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY в .env")
        return
    errs = []
    for attempt in (1, 2):
        for name in chain:
            got = False
            try:
                async for ev in _stream(name, messages, vision, thinking):
                    if ev[0] == "delta":
                        got = True
                    yield ev
                if got:
                    return
                errs.append(f"{name}: пустой поток")
            except Exception as e:  # httpx ошибки, LLMError, обрывы
                if got:
                    yield ("fail", f"{name}: обрыв потока: {type(e).__name__}: {e}")
                    return
                errs.append(f"{name}: {type(e).__name__}: {str(e)[:160]}")
        if attempt == 1:
            # 429/перегрузка — ждём дольше перед повтором; прочее — короткая пауза
            overloaded = any("429" in e or "overload" in e.lower() for e in errs)
            await asyncio.sleep(8 if overloaded else 1.5)  # разовая перегрузка/пустой поток — пробуем ещё раз
    yield ("fail", "Все провайдеры недоступны → " + " | ".join(errs[-3:]))


# ---------------------------------------------------------------- потоки для API

async def chat_stream(messages, mode: str = "CHAT", level: str = "LIGHT", images=None):
    """Обёртка: события dict + учёт токенов в бюджет."""
    prov, model_used, fail_msg = "?", "", ""
    text_parts, usage, t0 = [], None, time.time()
    call_msgs = list(messages)
    if budget.state() == "ECONOMY":
        call_msgs = call_msgs + [{"role": "system", "content": ECONOMY_NOTE}]
    try:
        async for kind, payload in llm_stream(call_msgs, level=level, purpose="chat", images=images):
            if kind == "provider":
                prov, _, model_used = payload.partition(":")
                yield {"t": "provider", "s": payload}
            elif kind == "delta":
                text_parts.append(payload)
                yield {"t": "delta", "s": payload}
            elif kind == "reason":
                yield {"t": "reason", "s": payload}
            elif kind == "usage":
                usage = payload
            elif kind == "fail":
                fail_msg = payload
                if not text_parts:
                    yield {"t": "err", "s": payload}  # настоящая причина вместо «пустой ответ»
                else:
                    yield {"t": "note", "s": payload}  # обрыв после части текста
    except Exception as e:
        budget.record(prov, model_used, 0, 0, mode, int((time.time() - t0) * 1000), ok=False)
        yield {"t": "err", "s": f"{type(e).__name__}: {e}"}
        return

    text = "".join(text_parts)
    if usage:
        p, c = usage
    else:  # fallback-оценка len/4
        p = budget.msgs_tokens(call_msgs)
        c = estimate_tokens(text)
    ok = bool(text.strip())
    budget.record(prov, model_used, p, c, mode, int((time.time() - t0) * 1000), ok=ok)
    yield {"t": "usage", "tok": int(p or 0) + int(c or 0)}
    if not ok and not fail_msg:
        yield {"t": "err", "s": "провайдер вернул пустой ответ (без причины)"}


# ---------------------------------------------------------------- агент-режим (единый)

SYSTEM_AGENT = """Ты — AresCode-V1, автономный ИИ-агент внутри терминала, создан RBares1337.
Если спрашивают про твою версию/модель/создателя — отвечай: «AresCode-V1, меня создал RBares1337», другие модели и провайдеры не называй. Проект: {project}.
Рабочая папка: /workspace/{project} (ты видишь только её).

САМ ВЫБИРАЙ РЕЖИМ ОТВЕТА:
* Вопрос, разговор, объяснение → ответь ОБЫЧНЫМ ТЕКСТОМ. Никакого JSON.
* Просят сделать работу (код, файлы, команды, установка/удаление софта, тесты, боты) → работай РОВНО ОДНИМ JSON-объектом на каждый ход, ответ начинается с первого символа {{, без ```-заборов и текста вокруг. Доступные действия:
{{"action":"plan","steps":["шаг 1","шаг 2"]}}      — краткий план перед нетривиальной работой
{{"action":"run","cmd":"python3 main.py"}}         — команда (python3 pip node npm git curl ffmpeg zip sqlite3 ls cat grep sed find и др.)
{{"action":"write","path":"rel/path.py","content":"..."}} — создать/перезаписать файл
{{"action":"edit","path":"rel/path.py","find":"точный фрагмент из файла","replace":"новый фрагмент"}} — точечная правка
{{"action":"read","path":"rel/path.py"}}           — прочитать файл (хвост 80 строк)
{{"action":"list","path":"."}}                     — дерево файлов проекта
{{"action":"done","summary":"итог для пользователя"}}
{{"action":"fail","reason":"почему не можешь"}}

Правила: для нетривиальных задач сначала action=plan; после каждого изменения запускай код, читай ошибки и исправляй сам; не выдумывай вывод команд; несколько правок одного файла — действием edit, а не переписыванием всего; пути только внутри проекта; доступ к .env закрыт.
Плагины/софт с GitHub: git clone --depth 1 <url> plugins/<имп>; зависимости — pip install --target=/workspace/.pylib -r plugins/<имп>/requirements.txt (PYTHONPATH уже настроен, импорты подхватятся). Удаление: rm -rf plugins/<имп>. Тяжёлые пакеты ставить некуда — если требуют компиляцию, сообщи в fail.
Telegram-боты: токен выдаёт @BotFather. Если в задаче токена нет — собери и проверь весь код (импорты, синтаксис), а токен запроси у пользователя в summary действия done."""

OB_BEGIN = "OBSERVATION (реальный вывод системы, не выдумывай):\n"


def _safe_path(project: str, rel: str):
    from pathlib import Path
    base = (config.WORKSPACE / project).resolve()
    p = (base / rel).resolve()
    if not str(p).startswith(str(base)):
        raise PermissionError(f"путь вне проекта: {rel}")
    if ".env" in p.parts or p.name == ".env":
        raise PermissionError("доступ к .env закрыт")
    return p


# --- инструменты агента: каждый возвращает (ok, показ_пользователю, наблюдение_модели) ---

async def _tool_run(cmd: str, project: str):
    res = await executor.run(str(cmd or ""), project)
    out = executor.filter_result(res, budget.out_lines())
    obs = f"{OB_BEGIN}exit={res['exit']}\nSTDOUT/OUT:\n{out}\nSTDERR:\n{res['err'][:1500]}"
    return res["exit"] == 0, res["cmd"], obs, out, res["exit"]


def _tool_write(project: str, rel: str, content: str):
    p = _safe_path(project, str(rel or ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    text = str(content or "")
    p.write_text(text, encoding="utf-8")
    lines = len(text.splitlines())
    disp = f"{rel} · +{lines} стр · {len(text.encode('utf-8'))} Б"
    obs = f"{OB_BEGIN}записано {len(text)} байт ({lines} строк) в {rel}"
    return True, disp, obs


def _tool_edit(project: str, rel: str, find: str, replace: str):
    p = _safe_path(project, str(rel or ""))
    src = p.read_text(encoding="utf-8", errors="replace")
    find_s = str(find or "")
    rep_s = str(replace if replace is not None else "")
    if not find_s:
        raise ValueError("пустой find — укажи точный фрагмент")
    cnt = src.count(find_s)
    if cnt == 0:
        raise ValueError("фрагмент не найден — сначала прочитай файл (read) и скопируй фрагмент точно")
    p.write_text(src.replace(find_s, rep_s, 1), encoding="utf-8")
    disp = f"{rel} · правка {len(find_s)}→{len(rep_s)} байт"
    obs = f"{OB_BEGIN}правка применена в {rel}: заменено первое из {cnt} вхождений"
    return True, disp, obs


def _tool_read(project: str, rel: str):
    p = _safe_path(project, str(rel or ""))
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(lines[-80:])
    disp = f"{rel} · {len(lines)} стр"
    obs = f"{OB_BEGIN}{rel} (хвост 80 строк):\n{body}"
    return True, disp, obs


def _tool_list(project: str, rel: str):
    from pathlib import Path
    base = _safe_path(project, str(rel or "."))
    SKIP = {"__pycache__", "node_modules", ".trash", ".tmp", ".git"}

    def tree(d: Path, pref: str, depth: int, out: list):
        if len(out) >= 80:
            return
        try:
            items = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for it in items:
            if len(out) >= 80:
                out.append("…")
                return
            if it.name in SKIP or (it.name.startswith(".") and it.name != ".gitignore"):
                continue
            if it.is_dir():
                out.append(f"{pref}{it.name}/")
                if depth > 0:
                    tree(it, pref + "  ", depth - 1, out)
            elif it.suffix != ".pyc":
                try:
                    sz = it.stat().st_size
                except OSError:
                    sz = 0
                out.append(f"{pref}{it.name} ({sz}Б)")

    entries: list = []
    if base.is_file():
        entries.append(f"{base.name} ({base.stat().st_size}Б)")
    else:
        tree(base, "", 1, entries)
    disp = f"{rel or '.'} · {len(entries)} объект(ов)"
    obs = f"{OB_BEGIN}дерево {rel or '.'}:\n" + ("\n".join(entries) or "(пусто)")
    return True, disp, obs


class _Disp:
    """Разделитель потока дельт: обычный текст стримится сразу, JSON-действие копится молча.

    Так сырой JSON-протокол никогда не попадает в терминал пользователю (баг «даёт JSON»)."""

    def __init__(self):
        self.buf = ""
        self.mode = None  # None — решаем | "text" | "json"

    def feed(self, s: str) -> str:
        """Вернуть часть, безопасную для показа пользователю."""
        if self.mode == "text":
            return s
        self.buf += s
        if self.mode is None:
            stripped = self.buf.lstrip()
            if not stripped:
                return ""
            if stripped[0] == "{":
                self.mode = "json"
            elif stripped.startswith("```json"):
                self.mode = "json"
            elif stripped.startswith("```"):
                self.mode = "text"  # обычный код-блок в ответе — показываем
            else:
                self.mode = "text"
        if self.mode == "text":
            show, self.buf = self.buf, ""
            return show
        return ""  # json — копим дальше

    def raw(self) -> str:
        """Полный накопленный поток (для разбора действия)."""
        return self.buf


async def agent_stream(goal: str, project: str = "default", level: str = "LIGHT", history=None):
    """Единый цикл агента: сам отвечает текстом ИЛИ работает инструментами.

    События — dict'ы для SSE. history — список прошлых ходов (мутируется: добавляем итог)."""
    st0 = budget.state()
    if st0 == "BLOCKED":
        yield {"t": "err", "s": f"ЛИМИТ ДНЯ ИСЧЕРПАН (100%) — новые задачи отклонены. {budget.hud()}"}
        return
    max_cycles = budget.max_iters(st0, level)
    max_turns = max_cycles * 2 + 8  # план/чтения не тратят циклы, но бесконечных ходов не бывает
    sys_prompt = SYSTEM_AGENT.format(project=project)
    if st0 == "ECONOMY":
        sys_prompt += ECONOMY_NOTE
    messages = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": str(goal)})
    cycles = turns = 0
    plan_text, active_files = "", set()
    yield {"t": "mode", "s": f"AGENT/{level} · циклы≤{max_cycles} · {budget.hud()}"}

    final_answer = ""

    while turns < max_turns:
        turns += 1
        if budget.compaction_needed(messages):
            messages = budget.compact(messages, plan_text, active_files)
            yield {"t": "note", "s": "контекст сжат (компакция)"}
        if budget.state() == "BLOCKED":
            yield {"t": "err", "s": "Лимит исчерпан посреди задачи — остановка. Продолжишь в новом дне или после поднятия лимита."}
            return

        disp, text = _Disp(), ""
        async for ev in chat_stream(messages, mode="AGENT", level=level):
            if ev["t"] == "delta":
                text += ev["s"]
                show = disp.feed(ev["s"])
                if show:
                    yield {"t": "delta", "s": show}
            elif ev["t"] in ("provider", "reason", "usage"):
                yield ev
            elif ev["t"] == "err":
                yield ev
                return

        if disp.mode != "json":
            # обычный текстовый ответ — ход завершён, сырой JSON никуда не попал
            final_answer = text.strip() or "(пустой ответ модели)"
            yield {"t": "done", "s": ""}
            _remember(history, goal, final_answer)
            return

        raw = disp.raw().strip()
        # если вокруг действия есть ```json-забор — снимем его
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        act = parse_action(raw)
        if act is None:
            # модель прислала JSON без action (например, пример) — показываем как текст
            final_answer = raw[:2000] or "(пустой ответ модели)"
            yield {"t": "delta", "s": final_answer}
            yield {"t": "done", "s": ""}
            _remember(history, goal, final_answer)
            return
        a = act.get("action")

        if a == "plan":
            plan_text = "\n".join(f"{i}. {s}" for i, s in enumerate(act.get("steps", []), 1))
            yield {"t": "plan", "s": plan_text}
            messages.append({"role": "assistant", "content": raw[:4000]})
            messages.append({"role": "user", "content": "План принят. Выполняй следующими JSON-действиями."})
            continue

        if a == "done":
            final_answer = str(act.get("summary", ""))[:2000]
            yield {"t": "done", "s": final_answer}
            _remember(history, goal, final_answer)
            return
        if a == "fail":
            final_answer = f"[задача не выполнена: {str(act.get('reason', ''))[:600]}]"
            yield {"t": "fail", "s": str(act.get("reason", ""))[:1000]}
            _remember(history, goal, final_answer)
            return

        ok, disp_s, obs = False, "", ""
        if a == "run":
            cycles += 1
            ok, disp_s, obs, out_txt, ex = await _tool_run(str(act.get("cmd", "")), project)
            yield {"t": "step", "kind": "run", "ok": ok, "s": disp_s, "exit": ex, "out": out_txt}
        elif a == "write":
            try:
                cycles += 1
                ok, disp_s, obs = _tool_write(project, str(act.get("path", "")), act.get("content", ""))
            except Exception as e:
                disp_s, obs = f"{act.get('path')}: {e}", f"{OB_BEGIN}ошибка записи: {e}"
            yield {"t": "step", "kind": "write", "ok": ok, "s": disp_s}
        elif a == "edit":
            try:
                cycles += 1
                ok, disp_s, obs = _tool_edit(project, str(act.get("path", "")),
                                             act.get("find", ""), act.get("replace", ""))
            except Exception as e:
                disp_s, obs = f"{act.get('path')}: {e}", f"{OB_BEGIN}ошибка правки: {e}"
            yield {"t": "step", "kind": "edit", "ok": ok, "s": disp_s}
        elif a == "read":
            try:
                ok, disp_s, obs = _tool_read(project, str(act.get("path", "")))
            except Exception as e:
                disp_s, obs = f"{act.get('path')}: {e}", f"{OB_BEGIN}ошибка чтения: {e}"
            yield {"t": "step", "kind": "read", "ok": ok, "s": disp_s}
        elif a == "list":
            try:
                ok, disp_s, obs = _tool_list(project, str(act.get("path", ".")))
            except Exception as e:
                disp_s, obs = f"{act.get('path')}: {e}", f"{OB_BEGIN}ошибка просмотра: {e}"
            yield {"t": "step", "kind": "list", "ok": ok, "s": disp_s}
        else:
            yield {"t": "note", "s": f"неизвестное действие '{a}' — жду plan/run/write/edit/read/list/done/fail"}
            obs = f"{OB_BEGIN}неизвестное действие '{a}'"

        messages.append({"role": "assistant", "content": raw[:4000]})
        messages.append({"role": "user", "content": obs})
        if cycles >= max_cycles:
            yield {"t": "note", "s": f"Достигнут лимит циклов ({max_cycles}) для {level}. Продолжить: режим ⚡MAX."}
            _remember(history, goal, "[остановлено по лимиту циклов]")
            return

    yield {"t": "note", "s": "Слишком много ходов — задача остановлена. Продолжи: «продолжай задачу»."}


def _remember(history, goal: str, answer: str):
    """Кладём итог хода в историю диалога (следующие вопросы видят контекст)."""
    if history is None:
        return
    history.append({"role": "user", "content": str(goal)[:2000]})
    history.append({"role": "assistant", "content": str(answer)[:2000]})
    if len(history) > 24:
        del history[:-20]


async def code_stream(goal: str, project: str = "default", level: str = "LIGHT"):
    """Совместимость: старое имя API — теперь это единый агент-поток."""
    async for ev in agent_stream(goal, project, level):
        yield ev


# ---------------------------------------------------------------- здоровье провайдеров

_hc = {"ts": 0.0, "data": None}


async def health(force: bool = False) -> dict:
    """Пинг каждого провайдера крошечным запросом (кэш 60с, в бюджет не пишем)."""
    if not force and _hc["data"] and time.time() - _hc["ts"] < 60:
        return _hc["data"]
    out = {}
    for name in _providers():
        t0 = time.time()

        async def _one():
            async for ev in _stream(name, [{"role": "user", "content": "ping"}], vision=False, thinking="disabled"):
                if ev[0] == "delta":
                    return True
            return False

        try:
            ok = await asyncio.wait_for(_one(), timeout=30)
            out[name] = {"ok": ok, "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            out[name] = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:120]}"}
    _hc.update(ts=time.time(), data=out)
    return out
