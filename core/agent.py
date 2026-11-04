"""ARES CODE — LLM-агент.

Failover-цепочка: GLM-4.7-Flash -> Gemini Flash -> OpenRouter :free-пул.
* GLM: OpenAI-compatible endpoint из GLM_API_BASE, параметр thinking
  {"type": "disabled"|"enabled"} — disabled для LIGHT/CHAT и шагов-исполнителей,
  enabled для планирования/отладки (STRONG/MAX).
* reasoning_content из ответа НЕ печатается в основной терминал — уходит отдельным
  событием "reason", UI показывает его тускло по клику на 🧠.
* vision: если в сообщении есть изображение — GLM переключается на GLM_VISION_MODEL
  (glm-4.6v-flash), у Gemini/OpenRouter картинка идёт нативным форматом.
* CODE-режим: цикл plan→run→fix на JSON-действиях, с компакцией между шагами.
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
            await asyncio.sleep(1.5)  # разовая перегрузка/пустой поток — пробуем ещё раз
    yield ("fail", "Все провайдеры недоступны → " + " | ".join(errs[-3:]))


# ---------------------------------------------------------------- потоки для API

async def chat_stream(messages, mode: str = "CHAT", level: str = "LIGHT", images=None):
    """Обёртка: события dict + учёт токенов в бюджет."""
    prov, model_used = "?", ""
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
    if not ok:
        yield {"t": "err", "s": "провайдер вернул пустой ответ"}


# ---------------------------------------------------------------- CODE-режим

SYSTEM_CODE = """Ты — ARES CODE, автономный dev-агент внутри терминала. Проект: {project}.
Рабочая папка: /workspace/{project} (ты видишь только её).
Отвечай РОВНО ОДНИМ JSON-объектом, без ```-заборов и пояснений вокруг. Доступные действия:
{{"action":"plan","steps":["шаг 1","шаг 2"]}}      — сначала всегда краткий план
{{"action":"run","cmd":"python3 main.py"}}         — выполнить команду (python3, pip, node, npm, git, curl, ffmpeg, zip, sqlite3 и базовые утилиты)
{{"action":"write","path":"rel/path.py","content":"..."}} — создать/перезаписать файл (путь относительно проекта)
{{"action":"read","path":"rel/path.py"}}           — прочитать файл
{{"action":"done","summary":"что сделано"}}

{{"action":"fail","reason":"почему не можешь"}}

Правила: пути только внутри проекта; после каждого изменения запускай код и читай ошибки, исправляй сам; не выдумывай вывод команд; будь экономным — без воды."""

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


async def code_stream(goal: str, project: str = "default", level: str = "LIGHT"):
    """Автономный цикл plan→run→fix. События — dict'ы для SSE."""
    st0 = budget.state()
    if st0 == "BLOCKED":
        yield {"t": "err", "s": f"ЛИМИТ ДНЯ ИСЧЕРПАН (100%) — новые задачи отклонены. {budget.hud()}"}
        return
    max_cycles = budget.max_iters(st0, level)
    sys_prompt = SYSTEM_CODE.format(project=project)
    if st0 == "ECONOMY":
        sys_prompt += ECONOMY_NOTE
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"ЗАДАЧА: {goal}\nНачни с action=plan."},
    ]
    plan_text, active_files, cycles = "", set(), 0
    yield {"t": "mode", "s": f"CODE/{level} · циклы≤{max_cycles} · {budget.hud()}"}

    while cycles < max_cycles:
        # компакция только на границе шагов — план→run→fix не рвём
        if budget.compaction_needed(messages):
            messages = budget.compact(messages, plan_text, active_files)
            yield {"t": "note", "s": "контекст сжат (компакция)"}
        if budget.state() == "BLOCKED":
            yield {"t": "err", "s": "Лимит исчерпан посреди задачи — остановка. Продолжишь в новом дне или после поднятия лимита."}
            return

        text = ""
        async for ev in chat_stream(messages, mode="CODE", level=level):
            if ev["t"] == "delta":
                text += ev["s"]
            elif ev["t"] in ("provider", "reason", "usage"):
                yield ev
            elif ev["t"] == "err":
                yield ev
                return

        act = parse_action(text)
        if act is None:
            yield {"t": "done", "s": (text.strip()[:2000]) or "(пустой ответ модели)"}
            return
        a = act.get("action")

        if a == "plan":
            plan_text = "\n".join(f"{i}. {s}" for i, s in enumerate(act.get("steps", []), 1))
            yield {"t": "plan", "s": plan_text}
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "План принят. Выполняй следующими JSON-действиями."})
            continue  # план не расходует цикл

        if a == "done":
            yield {"t": "done", "s": str(act.get("summary", ""))[:2000]}
            return
        if a == "fail":
            yield {"t": "fail", "s": str(act.get("reason", ""))[:1000]}
            return

        obs = ""
        if a == "run":
            cycles += 1
            res = await executor.run(str(act.get("cmd", "")), project)
            out = executor.filter_result(res, budget.out_lines())
            yield {"t": "step", "kind": "run", "ok": res["exit"] == 0, "s": res["cmd"],
                   "exit": res["exit"], "out": out}
            obs = f"{OB_BEGIN}exit={res['exit']}\nSTDOUT/OUT:\n{out}\nSTDERR:\n{res['err'][:1500]}"
        elif a == "write":
            try:
                p = _safe_path(project, str(act.get("path", "")))
                p.parent.mkdir(parents=True, exist_ok=True)
                content = str(act.get("content", ""))
                p.write_text(content, encoding="utf-8")
                active_files.add(str(act.get("path", "")))
                yield {"t": "step", "kind": "write", "ok": True, "s": str(act.get("path", ""))}
                obs = f"{OB_BEGIN}записано {len(content)} байт в {act.get('path')}"
            except Exception as e:
                yield {"t": "step", "kind": "write", "ok": False, "s": f"{act.get('path')}: {e}"}
                obs = f"{OB_BEGIN}ошибка записи: {e}"
        elif a == "read":
            try:
                p = _safe_path(project, str(act.get("path", "")))
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                body = "\n".join(lines[-80:])
                yield {"t": "step", "kind": "read", "ok": True, "s": str(act.get("path", ""))}
                obs = f"{OB_BEGIN}{act.get('path')} (хвост):\n{body}"
            except Exception as e:
                yield {"t": "step", "kind": "read", "ok": False, "s": f"{act.get('path')}: {e}"}
                obs = f"{OB_BEGIN}ошибка чтения: {e}"
        else:
            yield {"t": "note", "s": f"неизвестное действие '{a}' — жду plan/run/write/read/done/fail"}
            obs = f"{OB_BEGIN}неизвестное действие '{a}'"

        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": obs})

    yield {"t": "note", "s": f"Достигнут лимит циклов ({max_cycles}) для {level}. Продолжить: режим MAX."}


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
