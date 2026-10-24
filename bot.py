"""Puente Telegram ⇄ Claude Code en este PC.

Cada hilo guarda su session_id de Claude Code, así que cada mensaje reanuda la
misma conversación (--resume) en vez de empezar de cero.
"""
import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import free_router
import vercel_list

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "bridge.db"
INBOX = BASE / "inbox"
VIDEO_DIR = Path(os.environ.get("TEMP", BASE)) / "claude-bridge-videos"  # vídeos y sus fotogramas: temporales
CEREBRO = Path.home() / "cerebro"
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
CODEX = shutil.which("codex.cmd") or shutil.which("codex") or "codex"
HOME = str(Path.home())
SEND_RE = re.compile(r"\[\[ENVIAR:\s*(.+?)\]\]")
MAX_MSG = 4000

logging.basicConfig(filename=BASE / "data" / "bot.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")
# httpx registra cada URL de la API, que lleva el token dentro.
logging.getLogger("httpx").setLevel(logging.WARNING)


def load_env():
    values = {}
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


ENV = load_env()
USER_ID = int(ENV["TELEGRAM_USER_ID"])


# --- base de datos -----------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS threads (name TEXT PRIMARY KEY, session_id TEXT, cwd TEXT, updated REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY, created REAL, tool TEXT, "
                 "detail TEXT, reason TEXT, status TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, thread TEXT, ts REAL, engine TEXT, "
                 "role TEXT, text TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT, body TEXT, "
                 "priority INTEGER DEFAULT 3, status TEXT, origin TEXT, engine TEXT, created REAL, updated REAL, "
                 "summary TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, "
                 "sent INTEGER, text TEXT)")
    for table, column in (("threads", "codex_session"), ("tasks", "kind")):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError:
            pass  # ya existe
    return conn


def get_state(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))


def get_thread(name):
    with db() as conn:
        row = conn.execute("SELECT session_id, cwd FROM threads WHERE name=?", (name,)).fetchone()
        if not row:
            conn.execute("INSERT INTO threads (name, session_id, cwd, updated) VALUES (?, NULL, ?, ?)",
                         (name, HOME, time.time()))
            return None, HOME
    return row


def save_thread(name, session_id=None, cwd=None):
    get_thread(name)
    with db() as conn:
        if session_id is not None:
            conn.execute("UPDATE threads SET session_id=?, updated=? WHERE name=?",
                         (session_id or None, time.time(), name))
        if cwd is not None:
            conn.execute("UPDATE threads SET cwd=? WHERE name=?", (cwd, name))


def active_thread():
    return get_state("active_thread", "general")


def codex_session(name, value=None):
    """Lee (o guarda, si se pasa value) el thread_id de Codex del hilo."""
    get_thread(name)
    with db() as conn:
        if value is not None:
            conn.execute("UPDATE threads SET codex_session=? WHERE name=?", (value or None, name))
            return value
        return conn.execute("SELECT codex_session FROM threads WHERE name=?", (name,)).fetchone()[0]


def add_history(thread, engine, role, text):
    with db() as conn:
        conn.execute("INSERT INTO history (thread, ts, engine, role, text) VALUES (?,?,?,?,?)",
                     (thread, time.time(), engine, role, text[:4000]))


# --- elección de motor -------------------------------------------------------

CLAUDE_LIMIT = 0.97  # a partir de este uso de cualquier ventana, pasamos a Codex


ENGINE_COOLDOWN = 10 * 60  # pausa para un motor que falla sin ser por cupo


class LimitReached(Exception):
    pass


class EngineFailed(Exception):
    """El motor no ha dado respuesta (sin login, caído, error de red…)."""


class NoEngine(Exception):
    """Ningún motor ha podido responder."""


def claude_available():
    if time.time() < float(get_state("claude_blocked_until", "0") or 0):
        return False
    info = json.loads(get_state("rate_limit", "{}") or "{}")
    for window in (info.get("unifiedWindows") or {}).values():
        if window.get("utilization", 0) >= CLAUDE_LIMIT and (window.get("resetsAt") or 0) > time.time():
            return False
    return True


def pick_engine():
    mode = get_state("engine_mode", "auto")
    if mode in ("claude", "codex"):
        return mode
    return "claude" if claude_available() else "codex"


def handoff(thread, engine):
    """Si el hilo lo llevaba el otro motor, le pasamos los últimos intercambios."""
    if get_state(f"last_engine:{thread}") in (None, engine):
        return ""
    with db() as conn:
        rows = conn.execute("SELECT engine, role, text FROM history WHERE thread=? ORDER BY id DESC LIMIT 10",
                            (thread,)).fetchall()
    if not rows:
        return ""
    lines = [f"[{e} · {'usuario' if r == 'user' else 'respuesta'}] {t[:1500]}" for e, r, t in reversed(rows)]
    return ("(Relevo: este hilo lo venía llevando otro agente. Últimos intercambios para que sigas sin "
            "pedir contexto:)\n" + "\n".join(lines) + "\n(Fin del relevo.)\n\n")


# --- ejecución de Claude -----------------------------------------------------

class Runner:
    """Una cola y un proceso de Claude por hilo."""

    def __init__(self, name):
        self.name = name
        self.queue = asyncio.Queue()
        self.proc = None
        self.started = None
        self.action = ""
        self.engine = ""
        self.task = None


RUNNERS: dict[str, Runner] = {}


def runner(name):
    if name not in RUNNERS:
        RUNNERS[name] = Runner(name)
    r = RUNNERS[name]
    if r.task is None or r.task.done():
        r.task = asyncio.create_task(worker(r))
    return r


def describe_tool(block):
    name, inp = block.get("name", "?"), block.get("input", {})
    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or ""
    return f"{name}: {str(detail)[:120]}" if detail else name


async def run_claude(r: Runner, prompt: str, status_cb, on_text=None):
    session_id, cwd = get_thread(r.name)
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", "auto",
           "--settings", str(BASE / "guard_settings.json"),
           "--append-system-prompt-file", str(BASE / "system_prompt.md")]
    if session_id:
        cmd += ["--resume", session_id]

    r.proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env={**os.environ, "CLAUDE_BRIDGE": "1"},
                                                  stdin=asyncio.subprocess.PIPE,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                  limit=16 * 1024 * 1024)
    r.proc.stdin.write(prompt.encode("utf-8"))
    await r.proc.stdin.drain()
    r.proc.stdin.close()

    result, new_session, is_error, limit_status = None, None, False, {}
    stderr_task = asyncio.create_task(r.proc.stderr.read())
    async for raw in r.proc.stdout:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        new_session = event.get("session_id") or new_session
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    r.action = describe_tool(block)
                    await status_cb()
                elif block.get("type") == "text" and on_text:
                    await on_text(block.get("text", ""))
        elif kind == "rate_limit_event":
            limit_status = event.get("rate_limit_info", {})
            set_state("rate_limit", json.dumps(limit_status))
        elif kind == "result":
            result = event.get("result") or ""
            is_error = event.get("is_error", False)
    await r.proc.wait()
    stderr = (await stderr_task).decode("utf-8", "replace")
    code = r.proc.returncode
    r.proc = None

    if result is None:
        if session_id:
            # No se pudo retomar la sesión (borrada, de otra cuenta…): una nueva, una sola vez.
            save_thread(r.name, session_id="")
            return await run_claude(r, prompt, status_cb, on_text)
        # Sin respuesta (sin login, caído, cuenta cambiada…): lo apartamos un rato y que pruebe otro motor.
        set_state("claude_blocked_until", str(time.time() + ENGINE_COOLDOWN))
        raise EngineFailed(f"Claude sin respuesta (código {code}): {stderr[-300:].strip()}")
    if new_session:
        save_thread(r.name, session_id=new_session)
    if is_error and limit_status.get("status") == "rejected":
        set_state("claude_blocked_until", str(limit_status.get("resetsAt") or time.time() + 3600))
        raise LimitReached(result)
    return ("❌ " if is_error else "") + result


def codex_tool(item):
    kind = item.get("type", "")
    if kind == "command_execution":
        return f"Bash: {str(item.get('command', ''))[:120]}"
    if kind == "file_change":
        paths = [c.get("path", "") for c in item.get("changes", [])]
        return f"Edita: {', '.join(paths)[:120]}"
    return kind


async def run_codex(r: Runner, prompt: str, status_cb, on_text=None):
    session_id = codex_session(r.name)
    _, cwd = get_thread(r.name)
    common = ["--json", "--skip-git-repo-check", "--dangerously-bypass-hook-trust",
              "-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="never"']
    cmd = [CODEX, "exec"] + (["resume", session_id] if session_id else []) + common + ["-"]
    env = {**os.environ, "CLAUDE_BRIDGE": "1"}
    if not session_id:
        prompt = (BASE / "system_prompt.md").read_text(encoding="utf-8") + "\n\n" + prompt

    r.proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                  limit=16 * 1024 * 1024)
    r.proc.stdin.write(prompt.encode("utf-8"))
    await r.proc.stdin.drain()
    r.proc.stdin.close()

    messages, thread_id, error = [], None, None
    stderr_task = asyncio.create_task(r.proc.stderr.read())
    async for raw in r.proc.stdout:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "item.started":
            r.action = codex_tool(event.get("item", {}))
            await status_cb()
        elif kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            messages.append(event["item"].get("text", ""))
            if on_text:
                await on_text(messages[-1])
        elif kind in ("error", "turn.failed"):
            error = event.get("message") or json.dumps(event.get("error", event))[:500]
    await r.proc.wait()
    stderr = (await stderr_task).decode("utf-8", "replace")
    r.proc = None

    if thread_id:
        codex_session(r.name, thread_id)
    if messages:
        return messages[-1]
    if session_id and not thread_id:
        # El thread guardado ya no existe: empezamos uno nuevo una sola vez.
        codex_session(r.name, "")
        return await run_codex(r, prompt, status_cb, on_text)
    detail = error or stderr[-300:].strip()
    # Codex no informa de su cupo por adelantado: si es el límite, lo apartamos una hora; si no, un rato.
    pause = 3600 if "limit" in detail.lower() else ENGINE_COOLDOWN
    set_state("codex_blocked_until", str(time.time() + pause))
    raise EngineFailed(f"Codex sin respuesta: {detail}")


def codex_available():
    return time.time() >= float(get_state("codex_blocked_until", "0") or 0)


def engine_order(preferred=None):
    """Motores a probar, en orden. Con /motor fijado solo se usa ese."""
    mode = get_state("engine_mode", "auto")
    if mode in ("claude", "codex"):
        return [mode]
    first = preferred or pick_engine()
    available = {"claude": claude_available(), "codex": codex_available()}
    return [e for e in (first, "codex" if first == "claude" else "claude") if available[e]]


async def run_engine(r: Runner, prompt: str, status_cb, engine=None, on_text=None, allow_free=False):
    """Prueba los motores en orden (Claude → Codex, y opcionalmente un modelo gratuito al final).

    Devuelve (motor, texto). Lanza NoEngine si ninguno ha podido responder.
    """
    notes = []
    for current in engine_order(engine):
        r.engine = current
        try:
            if current == "claude":
                answer = await run_claude(r, handoff(r.name, "claude") + prompt, status_cb, on_text)
            else:
                answer = await run_codex(r, handoff(r.name, "codex") + prompt, status_cb, on_text)
        except LimitReached:
            notes.append(f"{current} sin cupo")
            continue
        except EngineFailed as exc:
            log.warning("%s", exc)
            notes.append(f"{current} no responde")
            continue
        set_state(f"last_engine:{r.name}", current)
        prefix = f"🔁 {', '.join(notes)}; sigo con {current}.\n\n" if notes else ""
        return current, prefix + answer
    if allow_free:
        try:
            label, text = await free_complete(prompt)
            return f"gratis·{label}", ("⚠️ Claude y Codex no están disponibles; respondo con un modelo gratuito, "
                                      "sin acceso a tu PC.\n\n" + text)
        except free_router.NoFreeModel:
            notes.append("modelos gratuitos sin cupo")
    raise NoEngine(", ".join(notes) or "ningún motor disponible")


async def worker(r: Runner):
    while True:
        prompt, chat_id, bot = await r.queue.get()
        r.started, r.action, r.engine = time.time(), "pensando", pick_engine()
        status = await bot.send_message(chat_id, f"⏳ [{r.name} · {r.engine}] trabajando…")
        last_edit = {"t": 0.0, "text": ""}

        async def status_cb():
            text = f"⏳ [{r.name} · {r.engine}] {int(time.time() - r.started)}s · {r.action}"
            if time.time() - last_edit["t"] > 6 and text != last_edit["text"]:
                last_edit.update(t=time.time(), text=text)
                try:
                    await status.edit_text(text)
                except BadRequest:
                    pass

        engine = r.engine
        try:
            engine, answer = await run_engine(r, prompt, status_cb, allow_free=True)
            add_history(r.name, engine, "user", prompt)
            add_history(r.name, engine, "assistant", answer)
        except NoEngine as exc:
            engine, answer = "puente", f"⛔ No hay ningún motor disponible ahora ({exc}). Prueba más tarde o /cupo."
        except Exception as exc:
            log.exception("fallo ejecutando %s", engine)
            answer = f"❌ Error interno: {exc}"
        elapsed = int(time.time() - r.started)
        r.started, r.action = None, ""
        await deliver(bot, chat_id, f"[{engine} · {elapsed}s] {answer}")
        try:
            await status.delete()  # el "trabajando…" solo sirve mientras espera: no deja un mensaje de más
        except BadRequest:
            pass
        r.queue.task_done()


# Lo único que llega al móvil sin haberlo pedido. El resto se guarda en notifications y se ve en /estado.
# Las autorizaciones las manda guard.py directamente.
NOTIFY_KINDS = {"implementado", "hecha", "bloqueada", "decision", "resumen"}


async def notify(bot, kind, text, reply_markup=None):
    sent = kind in NOTIFY_KINDS
    with db() as conn:
        conn.execute("INSERT INTO notifications (ts, kind, sent, text) VALUES (?,?,?,?)",
                     (time.time(), kind, int(sent), text))
    if not sent:
        log.info("aviso no enviado (%s): %s", kind, text[:200])
    elif reply_markup:
        await bot.send_message(USER_ID, text[:MAX_MSG], reply_markup=reply_markup)
    else:
        await deliver(bot, USER_ID, text)


async def deliver(bot, chat_id, text):
    files = SEND_RE.findall(text)
    text = SEND_RE.sub("", text).strip() or "(sin texto)"
    for i in range(0, len(text), MAX_MSG):
        await bot.send_message(chat_id, text[i:i + MAX_MSG])
    for f in files:
        await send_file(bot, chat_id, f.strip().strip('"'))


async def send_file(bot, chat_id, path):
    p = Path(path)
    if not p.is_file():
        await bot.send_message(chat_id, f"⚠️ No encuentro el archivo: {path}")
    elif p.stat().st_size > 50 * 1024 * 1024:
        await bot.send_message(chat_id, f"⚠️ {p.name} supera los 50 MB que permite Telegram.")
    else:
        with p.open("rb") as fh:
            await bot.send_document(chat_id, fh, filename=p.name)


def enqueue(context, chat_id, text):
    name = active_thread()
    header = f"[Telegram · {datetime.datetime.now():%Y-%m-%d %H:%M} · hilo {name}]\n"
    r = runner(name)
    busy = r.started is not None or not r.queue.empty()
    r.queue.put_nowait((header + text, chat_id, context.bot))
    return name, busy


# --- autonomía: cola de tareas y latidos -------------------------------------

TZ = ZoneInfo("Europe/Madrid")
AUTO_CLAUDE_5H, AUTO_CLAUDE_7D = 0.65, 0.90  # por encima, Claude queda reservado para el usuario
DEFAULT_HEARTBEAT_MIN = 120
HITO_RE = re.compile(r"\[\[HITO:\s*(.+?)\]\]", re.S)
PROPUESTA_RE = re.compile(r"\[\[PROPUESTA:\s*(.+?)\]\]", re.S)
ESTADO_RE = re.compile(r"ESTADO:\s*(HECHA|BLOQUEADA|CONTINUAR)", re.I)
OPEN_STATUSES = ("en curso", "pendiente", "bloqueada", "propuesta")


def add_title(body):
    return body.strip().splitlines()[0][:80]


def add_task(body, origin="usuario", status="pendiente", priority=3):
    title = add_title(body)
    with db() as conn:
        cur = conn.execute("INSERT INTO tasks (title, body, priority, status, origin, created, updated) "
                           "VALUES (?,?,?,?,?,?,?)", (title, body, priority, status, origin, time.time(), time.time()))
    return cur.lastrowid


def update_task(task_id, **fields):
    fields["updated"] = time.time()
    with db() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     (*fields.values(), task_id))


def get_task(task_id):
    with db() as conn:
        return conn.execute("SELECT id, title, body, status FROM tasks WHERE id=?", (task_id,)).fetchone()


def next_task():
    with db() as conn:
        return conn.execute("SELECT id, title, body, status FROM tasks WHERE status IN ('en curso', 'pendiente') "
                            "AND COALESCE(kind, 'agente') = 'agente' "
                            "ORDER BY status='en curso' DESC, priority DESC, created LIMIT 1").fetchone()


def window_usage(name):
    info = json.loads(get_state("rate_limit", "{}") or "{}")
    window = (info.get("unifiedWindows") or {}).get(name) or {}
    return window.get("utilization", 0) if (window.get("resetsAt") or 0) > time.time() else 0.0


def pick_auto_engine():
    """Motor para trabajo autónomo, más conservador que el interactivo. None = no hay cupo."""
    claude_ok = (claude_available() and window_usage("five_hour") < AUTO_CLAUDE_5H
                 and window_usage("seven_day") < AUTO_CLAUDE_7D)
    codex_ok = (time.time() >= float(get_state("codex_blocked_until", "0") or 0)
                and codex_usage("primary") < 0.97 and codex_usage("secondary") < 0.97)
    mode = get_state("engine_mode", "auto")
    if mode == "claude":
        return "claude" if claude_ok else None
    if mode == "codex":
        return "codex" if codex_ok else None
    return "claude" if claude_ok else ("codex" if codex_ok else None)


FREE_SYSTEM = ("Eres un asistente del usuario (español, directo, sin relleno). Contexto del usuario:\n{perfil}\n"
               "Responde en texto plano apto para Telegram.")
TRIAGE_PROMPT = ("Clasifica esta tarea. Responde SOLO una palabra:\n"
                 "SIMPLE si se puede resolver únicamente escribiendo texto con conocimiento general (redactar, resumir "
                 "un texto incluido en la tarea, traducir, explicar, ideas, borradores).\n"
                 "AGENTE si necesita tocar el PC, archivos, repositorios, ejecutar comandos, programar en un proyecto, "
                 "buscar en internet o contexto privado que no está en la tarea.\n\nTarea: {body}")


async def free_complete(prompt):
    perfil = (CEREBRO / "perfil.md").read_text(encoding="utf-8")
    return await asyncio.to_thread(free_router.complete, prompt, FREE_SYSTEM.format(perfil=perfil))


async def triage(body):
    """'simple' (lo hace un modelo gratuito) o 'agente' (Claude/Codex). Ante la duda, agente."""
    try:
        _, verdict = await asyncio.to_thread(free_router.complete, TRIAGE_PROMPT.format(body=body))
    except free_router.NoFreeModel:
        return "agente"
    return "simple" if verdict.strip().upper().startswith("SIMPLE") else "agente"


async def run_free_task(bot, task_id, title, body):
    try:
        label, text = await free_complete(body)
    except free_router.NoFreeModel as exc:
        log.warning("sin modelos gratuitos para #%s: %s", task_id, exc)
        update_task(task_id, kind="agente")  # que la haga Claude/Codex en el siguiente latido
        return
    update_task(task_id, status="hecha", engine=f"gratis·{label}", summary=text[:800])
    await notify(bot, "hecha", f"[gratis·{label}] ✅ #{task_id} {title}\n{text}")


async def run_task(bot, task, engine, note=None):
    if get_state("autonomy_paused", "0") == "1":
        return
    task_id, title, body, status = task
    name = f"auto-{task_id}"
    r = RUNNERS.setdefault(name, Runner(name))
    r.started, r.action, r.engine = time.time(), "empezando", engine
    update_task(task_id, status="en curso", engine=engine)
    instructions = (BASE / "autonomo_prompt.md").read_text(encoding="utf-8")
    if status == "pendiente":
        prompt = f"{instructions}\n\nTarea #{task_id}: {body}"
    else:
        prompt = f"{instructions}\n\nContinúa con la tarea #{task_id} donde la dejaste."
    if note:
        prompt += f"\n\nIndicaciones del usuario: {note}"

    async def quiet():
        pass

    hitos = []

    async def on_text(text):
        # Se juntan y se mandan al final en un solo aviso: si la tarea termina, van dentro del ✅ y no duplicados.
        hitos.extend(h.strip() for h in HITO_RE.findall(text))

    try:
        engine, answer = await run_engine(r, prompt, quiet, engine=engine, on_text=on_text)
        add_history(name, engine, "assistant", answer)
    except NoEngine as exc:
        log.warning("tarea %s sin motor: %s", task_id, exc)
        update_task(task_id, status=status)  # vuelve a la cola tal cual; el siguiente latido lo reintenta
        r.started, r.action = None, ""
        return
    except Exception as exc:
        log.exception("fallo en tarea autónoma %s", task_id)
        answer = f"❌ Error interno: {exc}"
    finally:
        r.started, r.action = None, ""

    for prop in PROPUESTA_RE.findall(answer):
        pid = add_task(prop.strip(), origin="propuesta", status="propuesta")
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aceptar", callback_data=f"prop:{pid}:ok"),
                                         InlineKeyboardButton("❌ Descartar", callback_data=f"prop:{pid}:no")]])
        await notify(bot, "decision", f"[{engine}] 💡 Propuesta #{pid}: {prop.strip()[:800]}", reply_markup=buttons)

    match = ESTADO_RE.search(answer)
    estado = match.group(1).upper() if match else "CONTINUAR"
    if answer.startswith("❌"):
        estado = "BLOQUEADA"
    summary = answer.split("RESUMEN:", 1)[1] if "RESUMEN:" in answer else answer[-800:]
    summary = HITO_RE.sub("", PROPUESTA_RE.sub("", summary)).strip()
    hitos = list(dict.fromkeys(hitos + [h.strip() for h in HITO_RE.findall(answer)]))
    logrado = ("\n\nLogrado:\n" + "\n".join(f"🏁 {h}" for h in hitos)) if hitos else ""
    if estado == "HECHA":
        update_task(task_id, status="hecha", summary=summary)
        await notify(bot, "hecha", f"[{engine}] ✅ #{task_id} {title}\n{summary}{logrado}")
    elif estado == "BLOQUEADA":
        update_task(task_id, status="bloqueada", summary=summary)
        await notify(bot, "bloqueada", f"[{engine}] ⚠️ #{task_id} {title} — bloqueada, necesito tu decisión:\n"
                                       f"{summary}{logrado}\n\nResponde con /reanudar {task_id} <indicaciones>")
    else:
        update_task(task_id, summary=summary)  # sigue en curso: lo retoma el siguiente latido
        if hitos:  # avance real a mitad de tarea (funcionalidad implementada): un único aviso
            await notify(bot, "implementado", f"[{engine}] 🏁 #{task_id} {title}:\n" + "\n".join(hitos))


async def heartbeat(context: ContextTypes.DEFAULT_TYPE):
    if get_state("autonomy_paused", "0") == "1":
        return
    if any(r.started for r in RUNNERS.values()):
        return  # hay algo en marcha; el siguiente latido lo retoma
    task = next_task()
    if not task:
        return  # nada que hacer: no se gasta cupo
    engine = pick_auto_engine()
    if not engine:
        if get_state("auto_paused_notified") != "1":
            set_state("auto_paused_notified", "1")
            await notify(context.bot, "info", "[puente] ⏸️ Hay tareas en cola pero no queda cupo para trabajo "
                                              "autónomo. Sigo cuando se libere.")
        return
    set_state("auto_paused_notified", "0")
    context.application.create_task(run_task(context.bot, task, engine))


def schedule_heartbeat(app):
    for job in app.job_queue.get_jobs_by_name("latido"):
        job.schedule_removal()
    minutes = int(get_state("heartbeat_minutes", str(DEFAULT_HEARTBEAT_MIN)))
    if minutes > 0:
        app.job_queue.run_repeating(heartbeat, interval=minutes * 60, first=60, name="latido")
    return minutes


async def daily_summary(context: ContextTypes.DEFAULT_TYPE, manual=False):
    if get_state("autonomy_paused", "0") == "1" and not manual:
        return  # en pausa no sale solo; /resumen lo da igualmente
    since = time.time() - 24 * 3600
    with db() as conn:
        rows = conn.execute("SELECT id, title, status, engine FROM tasks WHERE updated >= ? ORDER BY status",
                            (since,)).fetchall()
        pending = conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pendiente', 'en curso')").fetchone()[0]
    icons = {"hecha": "✅", "bloqueada": "⚠️", "en curso": "🔄", "pendiente": "🕓", "propuesta": "💡",
             "descartada": "🗑️"}
    lines = ["[puente] 📋 Resumen de las últimas 24 h"]
    lines += [f"{icons.get(s, '•')} #{i} {t} ({s}{', ' + e if e else ''})" for i, t, s, e in rows] or ["Sin actividad."]
    lines.append(f"\nEn cola: {pending}")
    yesterday = (datetime.datetime.now(TZ) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    diary = CEREBRO / "diario" / f"{yesterday}.md"
    if diary.exists():
        lines.append(f"\nDiario de ayer:\n{diary.read_text(encoding='utf-8').strip()[:2500]}")
    lines += silent_notifications(since)
    lines.append("\n" + cupo_text())
    await notify(context.bot, "resumen", "\n".join(lines))


def silent_notifications(since, limit=5):
    """Avisos que no se mandaron al móvil (no eran hito, fin, bloqueo ni decisión), para /estado y el resumen."""
    with db() as conn:
        rows = conn.execute("SELECT ts, text FROM notifications WHERE sent=0 AND ts >= ? ORDER BY ts DESC LIMIT ?",
                            (since, limit)).fetchall()
    if not rows:
        return []
    return ["\nAvisos sin enviar al móvil:"] + [
        f"• {datetime.datetime.fromtimestamp(ts, TZ):%d/%m %H:%M} {text.splitlines()[0][:150]}" for ts, text in rows]


# --- orquestación: Claude + Codex en paralelo sobre un mismo objetivo --------

ORQ_DIR = BASE / "orquesta"
JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
MAX_ROUNDS = 4  # veces que se le pide "continúa" a un agente antes de darlo por bloqueado

PLAN_PROMPT = """Eres el ORQUESTADOR de un objetivo que van a desarrollar EN PARALELO dos agentes: "claude" y "codex".
Objetivo del usuario: {goal}
Repositorio: {repo} (rama base: {base})

1. Explora el repositorio lo justo para entenderlo. No modifiques nada.
2. Divide el objetivo en 2-4 subtareas independientes y repártelas entre "claude" y "codex" (los dos deben trabajar).
3. Cada subtarea debe tocar archivos DISTINTOS de las demás, para que no haya conflictos al unirlas.
4. Si el objetivo es demasiado pequeño para dividirlo, devuelve una sola subtarea.

Responde SOLO con un bloque ```json con esta forma:
{{"resumen": "plan en 1-2 líneas", "subtareas": [{{"id": "a", "titulo": "...", "agente": "claude",
"archivos": ["ruta/..."], "brief": "instrucciones completas y autosuficientes"}}],
"verificacion": "cómo comprobar el resultado final (tests, build...)"}}"""

SUB_PROMPT = """Eres el agente {agent} y trabajas en UNA subtarea de un objetivo orquestado; otros agentes trabajan a la vez.
Objetivo global: {goal}
Tu subtarea ({sid}): {title}
Instrucciones: {brief}
Solo puedes modificar: {files}.
Estás en tu propio worktree ({cwd}), rama {branch}. Haz commits en ESTA rama (mensajes en español). NO hagas push ni merge.
Al terminar, escribe en {coord}/NOTAS-{sid}.md qué has hecho y lo que deba saber el integrador.
No informes de pasos pequeños; solo un [[HITO: ...]] si logras algo importante.
NO escribas en el cerebro (diario/proyectos): lo hace el integrador al final.
Termina SIEMPRE con:
ESTADO: HECHA | BLOQUEADA | CONTINUAR
RESUMEN: máximo 3 líneas"""

REVIEW_PROMPT = """Revisa SIN modificar código el trabajo de otro agente: rama {branch} del repositorio {repo}
(cambios: `git -C "{repo}" diff {base}...{branch}`). Objetivo global: {goal}. Su subtarea: {title}.
Busca bugs, incumplimientos de la subtarea y riesgos. Escribe hallazgos concretos y breves en {coord}/REVISION-{sid}.md
(si todo está bien, dilo). NO escribas en el cerebro. Responde con un resumen de 2 líneas."""

INTEGRATE_PROMPT = """Eres el INTEGRADOR del objetivo: {goal}
Estás en el worktree {cwd}, rama {branch} (creada desde {base}).
1. Haz `git merge --no-ff` de estas ramas: {branches}. Resuelve los conflictos si los hay.
2. Lee las notas y revisiones de {coord} (NOTAS-*.md, REVISION-*.md) y corrige lo que esté justificado.
3. Verifica: {verify}
4. Haz commit en esta rama. NO hagas push ni merge a {base}.
5. Añade UNA sola línea al diario de hoy del cerebro con el resultado (y actualiza el proyecto solo si cambia algo importante).
Termina SIEMPRE con:
ESTADO: HECHA | BLOQUEADA | CONTINUAR
RESUMEN: máximo 4 líneas: qué hace ahora la rama, resultado de la verificación y qué falta."""


def git(repo, *args):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    if out.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def preferred(agent):
    """Respeta el reparto del plan, salvo que el presupuesto autónomo no permita usar ese motor."""
    if agent == "claude" and pick_auto_engine() != "claude":
        return "codex"
    return agent if agent in ("claude", "codex") else (pick_auto_engine() or pick_engine())


async def agent_until_done(bot, name, cwd, engine, prompt, label):
    """Lanza un agente en su propio hilo y le pide continuar hasta HECHA/BLOQUEADA. Devuelve (motor, estado, resumen)."""
    save_thread(name, cwd=str(cwd))
    r = RUNNERS.setdefault(name, Runner(name))
    r.started = time.time()

    async def quiet():
        pass

    seen = set()

    async def on_text(text):
        for hito in (h.strip() for h in HITO_RE.findall(text)):
            if hito not in seen:  # el mismo hito vuelve a salir en la respuesta final
                seen.add(hito)
                await notify(bot, "implementado", f"[{r.engine}] 🏁 {label}: {hito}")

    used, answer = engine, ""
    try:
        for attempt in range(MAX_ROUNDS):
            used, answer = await run_engine(r, prompt if attempt == 0 else "Continúa donde lo dejaste.", quiet,
                                            engine=engine, on_text=on_text)
            match = ESTADO_RE.search(answer)
            if match and match.group(1).upper() != "CONTINUAR":
                break
    finally:
        r.started, r.action = None, ""
    match = ESTADO_RE.search(answer)
    estado = match.group(1).upper() if match else "BLOQUEADA"
    if estado == "CONTINUAR":
        estado = "BLOQUEADA"  # se le acabaron los turnos
    summary = answer.split("RESUMEN:", 1)[1].strip() if "RESUMEN:" in answer else answer[-600:]
    return used, estado, HITO_RE.sub("", summary).strip()


async def orchestrate(bot, task_id, goal, repo):
    title = add_title(goal)
    coord = ORQ_DIR / str(task_id)
    coord.mkdir(parents=True, exist_ok=True)

    async def quiet():
        pass

    try:
        repo = Path(git(repo, "rev-parse", "--show-toplevel"))
        base = git(repo, "branch", "--show-current") or git(repo, "rev-parse", "HEAD")
        lead = pick_auto_engine() or pick_engine()

        # 1. Plan
        planner = f"orq{task_id}-plan"
        save_thread(planner, cwd=str(repo))
        r = RUNNERS.setdefault(planner, Runner(planner))
        r.started = time.time()
        try:
            lead, answer = await run_engine(r, PLAN_PROMPT.format(goal=goal, repo=repo, base=base), quiet, engine=lead)
        finally:
            r.started, r.action = None, ""
        match = JSON_RE.search(answer) or re.search(r"(\{.*\})", answer, re.S)
        if not match:
            raise RuntimeError(f"el orquestador no devolvió un plan válido: {answer[:300]}")
        plan = json.loads(match.group(1))
        subs = plan["subtareas"]
        (coord / "PLAN.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        await notify(bot, "info", f"[{lead}] 🧭 Objetivo #{task_id}: plan listo. {plan.get('resumen', '')}\n" +
                     "\n".join(f"• {s['id']} → {s['agente']}: {s['titulo']}" for s in subs))

        # 2. Subtareas en paralelo, cada una en su worktree y su rama
        jobs = []
        for s in subs:
            s["branch"] = f"orq/{task_id}-{s['id']}"
            wt = repo.parent / f"{repo.name}-orq{task_id}-{s['id']}"
            if not wt.exists():
                git(repo, "worktree", "add", str(wt), "-b", s["branch"], base)
            prompt = SUB_PROMPT.format(agent=s["agente"], goal=goal, sid=s["id"], title=s["titulo"], brief=s["brief"],
                                       files=", ".join(s.get("archivos", [])) or "lo necesario para tu subtarea",
                                       cwd=wt, branch=s["branch"], coord=coord)
            jobs.append(agent_until_done(bot, f"orq{task_id}-{s['id']}", wt, preferred(s["agente"]), prompt,
                                         f"#{task_id}/{s['id']} {s['titulo']}"))
        for s, res in zip(subs, await asyncio.gather(*jobs, return_exceptions=True)):
            if isinstance(res, Exception):
                s["motor"], s["estado"], s["resumen"] = s["agente"], "BLOQUEADA", str(res)
            else:
                s["motor"], s["estado"], s["resumen"] = res
        done = [s for s in subs if s["estado"] == "HECHA"]
        if not done:
            raise RuntimeError("ninguna subtarea terminó: " + " | ".join(f"{s['id']}: {s['resumen'][:150]}" for s in subs))

        # 3. Revisión cruzada en paralelo: cada rama la revisa el otro motor
        reviews = []
        for s in done:
            name = f"orq{task_id}-rev-{s['id']}"
            save_thread(name, cwd=str(repo))
            reviewer = "codex" if s["motor"] == "claude" else preferred("claude")
            reviews.append(run_engine(RUNNERS.setdefault(name, Runner(name)),
                                      REVIEW_PROMPT.format(branch=s["branch"], repo=repo, base=base, goal=goal,
                                                           title=s["titulo"], coord=coord, sid=s["id"]),
                                      quiet, engine=reviewer))
        await asyncio.gather(*reviews, return_exceptions=True)

        # 4. Integración en una rama aparte (nunca en la base)
        ibranch = f"orq/{task_id}-integracion"
        iwt = repo.parent / f"{repo.name}-orq{task_id}-integracion"
        if not iwt.exists():
            git(repo, "worktree", "add", str(iwt), "-b", ibranch, base)
        used, estado, summary = await agent_until_done(
            bot, f"orq{task_id}-int", iwt, lead,
            INTEGRATE_PROMPT.format(goal=goal, cwd=iwt, branch=ibranch, base=base, coord=coord,
                                    branches=", ".join(s["branch"] for s in done),
                                    verify=plan.get("verificacion") or "compila y pasa los tests si los hay"),
            f"#{task_id} integración")
        (coord / "PLAN.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        update_task(task_id, status="hecha" if estado == "HECHA" else "bloqueada", engine=used, summary=summary)
        lines = [f"[{used}] {'✅' if estado == 'HECHA' else '⚠️'} Objetivo #{task_id} {title}", summary, "", "Subtareas:"]
        lines += [f"{'✅' if s['estado'] == 'HECHA' else '⚠️'} {s['id']} [{s['motor']}] {s['titulo']}" for s in subs]
        lines += ["", f"Resultado en la rama {ibranch} ({iwt}). No he hecho push ni merge a {base}.",
                  f"Plan, notas y revisiones: {coord}"]
        await notify(bot, "hecha" if estado == "HECHA" else "bloqueada", "\n".join(lines))
    except NoEngine as exc:
        update_task(task_id, status="bloqueada", summary=str(exc))
        await notify(bot, "bloqueada", f"[puente] ⏸️ Objetivo #{task_id} bloqueado: no hay motor disponible ({exc}). "
                                       f"/reanudar no aplica; vuelve a lanzarlo con /objetivo cuando haya cupo.")
    except Exception as exc:
        log.exception("fallo orquestando %s", task_id)
        update_task(task_id, status="bloqueada", summary=str(exc))
        await notify(bot, "bloqueada", f"[puente] ⚠️ Objetivo #{task_id} bloqueado, necesito tu decisión: {exc}")


# --- transcripción -----------------------------------------------------------

_whisper = None


def transcribe(path):
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(str(path), language="es", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


# --- handlers ----------------------------------------------------------------

def help_text():
    lines = [f"/{name} {args}".rstrip() + f" — {desc}" for name, _, args, desc in COMMANDS]
    return "Comandos:\n" + "\n".join(lines) + "\n\nO escríbeme, mándame audios o archivos sin más."


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Hola. Hilo activo: {active_thread()}\n\n{help_text()}")


def fmt_reset(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %d %H:%M") if ts else "?"


def cupo_text():
    info = json.loads(get_state("rate_limit", "{}") or "{}")
    windows = info.get("unifiedWindows") or {}
    labels = {"five_hour": "Ventana 5 h", "seven_day": "Semanal"}
    lines = [f"{labels.get(k, k)}: {round(v.get('utilization', 0) * 100)}% · reinicia {fmt_reset(v.get('resetsAt'))}"
             if (v.get("resetsAt") or 0) > time.time() else f"{labels.get(k, k)}: 0% (ya se reinició)"
             for k, v in windows.items()]
    claude = ("Cupo Claude\n" + "\n".join(lines)) if lines else "Cupo Claude: sin datos todavía."
    return claude + "\n\n" + codex_cupo_text()


def codex_rate_limits():
    """Último rate_limits que Codex anotó en sus sesiones (~/.codex/sessions). (datos, segundos de antigüedad) o None."""
    files = sorted((Path.home() / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    for path in files:
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                limits = (json.loads(line).get("payload") or {}).get("rate_limits")
            except json.JSONDecodeError:
                continue
            if limits:
                return limits, time.time() - path.stat().st_mtime
    return None


def codex_usage(window):
    """Fracción usada de la ventana 'primary' (5 h) o 'secondary' (semanal); 0 si ya se reinició o no hay datos."""
    found = codex_rate_limits()
    w = (found[0].get(window) or {}) if found else {}
    return w.get("used_percent", 0) / 100 if (w.get("resets_at") or 0) > time.time() else 0.0


def codex_cupo_text():
    found = codex_rate_limits()
    if not found:
        return "Cupo Codex: sin datos todavía (se actualiza cada vez que trabaja)."
    limits, age = found
    labels = {"primary": "Ventana 5 h", "secondary": "Semanal"}
    lines = []
    for key, label in labels.items():
        w = limits.get(key) or {}
        if (w.get("resets_at") or 0) > time.time():
            lines.append(f"{label}: {round(w.get('used_percent', 0))}% · reinicia {fmt_reset(w.get('resets_at'))}")
        elif w:
            lines.append(f"{label}: 0% (ya se reinició)")
    blocked = float(get_state("codex_blocked_until", "0") or 0)
    if blocked > time.time():
        lines.append(f"⛔ Apartado hasta {fmt_reset(blocked)}")
    return f"Cupo Codex (dato de hace {fmt_age(age)})\n" + "\n".join(lines)


def fmt_age(seconds):
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


async def cmd_cupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(cupo_text())


async def cmd_vercel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        messages = await asyncio.to_thread(vercel_list.list_messages, ENV.get("VERCEL_SCOPE") or None)
    except vercel_list.VercelListError as exc:
        await update.message.reply_text(str(exc))
        return
    for message in messages:
        await update.message.reply_text(message, disable_web_page_preview=True)


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"Hilo activo: {active_thread()} · motor {get_state('engine_mode', 'auto')} (usaría {pick_engine()})"]
    for name, r in RUNNERS.items():
        if r.started:
            lines.append(f"• {name} [{r.engine}]: trabajando {int(time.time() - r.started)}s · {r.action} · "
                         f"en cola {r.queue.qsize()}")
        else:
            lines.append(f"• {name}: libre")
    with db() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    if pending:
        lines.append(f"⚠️ {pending} confirmación(es) pendiente(s)")
    lines += silent_notifications(time.time() - 24 * 3600)
    lines += ["", cupo_text()]
    await update.message.reply_text("\n".join(lines))


async def cmd_hilo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        with db() as conn:
            rows = conn.execute("SELECT name, cwd, session_id IS NOT NULL FROM threads ORDER BY updated DESC").fetchall()
        current = active_thread()
        body = "\n".join(f"{'▶' if n == current else '•'} {n} — {c}{'' if s else ' (nuevo)'}" for n, c, s in rows)
        await update.message.reply_text(f"Hilos:\n{body or '(ninguno)'}\n\nUso: /hilo nombre [ruta]")
        return
    name = context.args[0].lower()
    get_thread(name)
    if len(context.args) > 1:
        cwd = " ".join(context.args[1:])
        if not Path(cwd).is_dir():
            await update.message.reply_text(f"⚠️ No existe la carpeta {cwd}")
            return
        save_thread(name, cwd=cwd)
    set_state("active_thread", name)
    _, cwd = get_thread(name)
    await update.message.reply_text(f"▶ Hilo activo: {name} (carpeta {cwd})")


async def cmd_motor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].lower() == "reset":
        # Tras cambiar de cuenta: olvida cupos y pausas de la cuenta anterior.
        for key in ("rate_limit", "claude_blocked_until", "codex_blocked_until", "auto_paused_notified"):
            set_state(key, "")
        await update.message.reply_text("♻️ Cupos y pausas olvidados; se vuelven a medir con el próximo mensaje.")
    if context.args and context.args[0].lower() in ("auto", "claude", "codex"):
        set_state("engine_mode", context.args[0].lower())
    mode = get_state("engine_mode", "auto")
    await update.message.reply_text(
        f"Motor: {mode} (ahora mismo usaría {pick_engine()}).\n"
        "auto = Claude, y Codex cuando Claude se queda sin cupo · /motor claude · /motor codex")


async def cmd_tarea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Uso: /tarea <qué hay que hacer>")
        return
    task_id = add_task(text)
    kind = await triage(text)
    update_task(task_id, kind=kind)
    if kind == "simple":
        await update.message.reply_text(f"📥 Tarea #{task_id}: sencilla, la resuelve un modelo gratuito ahora.")
        context.application.create_task(run_free_task(context.bot, task_id, add_title(text), text))
        return
    jobs = context.application.job_queue.get_jobs_by_name("latido")
    when = f"próximo latido {jobs[0].next_t.astimezone(TZ):%H:%M}" if jobs else "latidos apagados (/latido ahora)"
    await update.message.reply_text(f"📥 Tarea #{task_id} en cola para Claude/Codex · {when}")


async def cmd_objetivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not Path(context.args[0]).is_dir():
        await update.message.reply_text("Uso: /objetivo <ruta del repo> <qué hay que conseguir>\n"
                                        "Ej.: /objetivo C:\\ruta\\a\\tu\\repo añadir exportación a CSV")
        return
    repo, goal = context.args[0], " ".join(context.args[1:])
    task_id = add_task(goal)
    update_task(task_id, kind="objetivo", status="en curso")
    context.application.create_task(orchestrate(context.bot, task_id, goal, repo))
    await update.message.reply_text(f"🧭 Objetivo #{task_id} en marcha. Te aviso con el plan, los hitos y el resultado; "
                                    f"nada de pasos intermedios. /estado para ver quién trabaja.")


async def cmd_gratis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Uso: /gratis <pregunta> (modelo gratuito, sin acceso al PC)")
        return
    try:
        label, answer = await free_complete(text)
    except free_router.NoFreeModel as exc:
        await update.message.reply_text(f"[gratis] ❌ Ningún modelo gratuito disponible ahora: {exc}")
        return
    await deliver(context.bot, update.effective_chat.id, f"[gratis·{label}] {answer}")


async def cmd_cola(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute(f"SELECT id, title, status, priority, engine FROM tasks WHERE status IN "
                            f"({','.join('?' * len(OPEN_STATUSES))}) ORDER BY status='en curso' DESC, priority DESC, "
                            f"created", OPEN_STATUSES).fetchall()
        done = conn.execute("SELECT id, title FROM tasks WHERE status='hecha' ORDER BY updated DESC LIMIT 5").fetchall()
    body = "\n".join(f"#{i} [{s} · p{p}{' · ' + e if e else ''}] {t}" for i, t, s, p, e in rows) or "(vacía)"
    if done:
        body += "\n\nÚltimas hechas:\n" + "\n".join(f"✅ #{i} {t}" for i, t in done)
    await update.message.reply_text(f"Cola de tareas:\n{body}\n\n/prioridad id 1-5 (5 = máxima) · /descartar id")


async def cmd_prioridad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        task_id, prio = int(context.args[0]), max(1, min(5, int(context.args[1])))
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: /prioridad <id> <1-5>")
        return
    update_task(task_id, priority=prio)
    await update.message.reply_text(f"#{task_id} → prioridad {prio}")


async def cmd_descartar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        task_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: /descartar <id>")
        return
    update_task(task_id, status="descartada")
    await update.message.reply_text(f"🗑️ #{task_id} descartada")


async def cmd_reanudar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        task = get_task(int(context.args[0]))
    except (IndexError, ValueError):
        task = None
    if not task:
        await update.message.reply_text("Uso: /reanudar <id> [indicaciones]")
        return
    engine = pick_auto_engine() or pick_engine()
    status = "en curso" if task[3] in ("en curso", "bloqueada") else "pendiente"
    context.application.create_task(run_task(context.bot, (*task[:3], status), engine,
                                             note=" ".join(context.args[1:]) or None))
    await update.message.reply_text(f"▶️ Retomo #{task[0]} con {engine}. Te aviso solo de hitos o al terminar.")


async def cmd_latido(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg == "ahora":
        if get_state("autonomy_paused", "0") == "1":
            await update.message.reply_text("⏸️ La autonomía está en pausa: actívala con /autonomia on.")
            return
        await heartbeat(context)
        await update.message.reply_text("💓 Latido lanzado.")
        return
    if arg == "off":
        set_state("heartbeat_minutes", "0")
    elif re.fullmatch(r"\d+[mh]", arg):
        set_state("heartbeat_minutes", str(int(arg[:-1]) * (60 if arg.endswith("h") else 1)))
    minutes = schedule_heartbeat(context.application)
    status = f"cada {minutes} min" if minutes else "apagado"
    await update.message.reply_text(f"💓 Latido {status}. Uso: /latido 2h · /latido 45m · /latido off · /latido ahora\n"
                                    f"Claude en autónomo solo si 5 h < {AUTO_CLAUDE_5H:.0%} y semanal < "
                                    f"{AUTO_CLAUDE_7D:.0%}; si no, Codex.")


async def cmd_autonomia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        set_state("autonomy_paused", "0" if arg == "on" else "1")
        set_state("autonomy_pause_reason", "" if arg == "on" else "Pausada por el usuario con /autonomia off")
        if arg == "on" and get_state("heartbeat_minutes", str(DEFAULT_HEARTBEAT_MIN)) == "0":
            set_state("heartbeat_minutes", str(DEFAULT_HEARTBEAT_MIN))  # sin latido no trabajaría nunca
        schedule_heartbeat(context.application)
        if arg == "off":
            for job in context.application.job_queue.get_jobs_by_name("latido"):
                job.schedule_removal()
    paused = get_state("autonomy_paused", "0") == "1"
    minutes = int(get_state("heartbeat_minutes", str(DEFAULT_HEARTBEAT_MIN)))
    with db() as conn:
        queued = conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pendiente', 'en curso') "
                              "AND COALESCE(kind, 'agente') = 'agente'").fetchone()[0]
    text = (f"⏸️ Autonomía en pausa. {get_state('autonomy_pause_reason', '') or ''}".strip() if paused else
            f"▶️ Autonomía activa: latido cada {minutes} min, {queued} tarea(s) en cola.")
    await update.message.reply_text(f"{text}\nUso: /autonomia on · /autonomia off (lo que esté en marcha termina "
                                    f"su turno; no empieza nada nuevo)")


async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await daily_summary(context, manual=True)


async def cmd_nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_thread(active_thread(), session_id="")
    codex_session(active_thread(), "")
    await update.message.reply_text(f"🆕 El hilo {active_thread()} empieza de cero (el cerebro se mantiene).")


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = RUNNERS.get(active_thread())
    if not r or not r.proc:
        await update.message.reply_text("No hay nada ejecutándose en este hilo.")
        return
    while not r.queue.empty():
        r.queue.get_nowait()
        r.queue.task_done()
    subprocess.run(["taskkill", "/PID", str(r.proc.pid), "/T", "/F"], capture_output=True)
    await update.message.reply_text("🛑 Cancelado.")


async def cmd_enviar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /enviar C:\\ruta\\al\\archivo")
        return
    await send_file(context.bot, update.effective_chat.id, " ".join(context.args).strip('"'))


async def cmd_recuerda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Uso: /recuerda <texto>")
        return
    with (CEREBRO / "notas.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- ({datetime.date.today()}) {text}\n")
    await update.message.reply_text("🧠 Guardado en el cerebro.")


async def cmd_contexto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = []
    for f in ("perfil.md", "notas.md"):
        p = CEREBRO / f
        if p.exists():
            parts.append(p.read_text(encoding="utf-8").strip())
    projects = sorted(p.stem for p in (CEREBRO / "proyectos").glob("*.md"))
    parts.append("Proyectos: " + ", ".join(projects))
    await deliver(context.bot, update.effective_chat.id, "\n\n".join(parts))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name, busy = enqueue(context, update.effective_chat.id, update.message.text)
    if busy:
        await update.message.reply_text(f"📥 En cola en el hilo {name}.")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    media = update.message.voice or update.message.audio
    tg_file = await media.get_file()
    path = INBOX / f"audio_{datetime.datetime.now():%Y%m%d_%H%M%S}.ogg"
    await tg_file.download_to_drive(path)
    note = await update.message.reply_text("🎙️ Transcribiendo…")
    try:
        text = await asyncio.to_thread(transcribe, path)
    except Exception as exc:
        log.exception("fallo transcribiendo")
        await note.edit_text(f"❌ No pude transcribir el audio: {exc}")
        return
    if not text:
        await note.edit_text("🎙️ No he entendido nada en el audio.")
        return
    await note.edit_text(f"🎙️ «{text}»")
    name, busy = enqueue(context, update.effective_chat.id, f"(mensaje de voz transcrito) {text}")
    if busy:
        await update.message.reply_text(f"📥 En cola en el hilo {name}.")


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media = msg.document or msg.video or msg.video_note or msg.animation
    size = getattr(media, "file_size", 0) or 0
    if size > 20 * 1024 * 1024:  # límite de descarga de la API de bots de Telegram
        await msg.reply_text(f"⚠️ El archivo pesa {size // (1024 * 1024)} MB y los bots de Telegram solo pueden "
                             "descargar hasta 20 MB. Mándalo más corto o comprimido, o pásame un enlace.")
        return
    if media:
        filename = getattr(media, "file_name", None) or ("archivo" if media is msg.document else "video.mp4")
        tg_file = await media.get_file()
    else:
        tg_file, filename = await msg.photo[-1].get_file(), "foto.jpg"
    is_video = Path(filename).suffix.lower() in (".mp4", ".mov", ".webm", ".m4v")
    # Los vídeos van a la carpeta temporal: se borran al terminar sin pedir confirmación y no llenan el disco.
    folder = VIDEO_DIR if is_video else INBOX
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_{filename}"
    await tg_file.download_to_drive(path)
    caption = msg.caption or "(sin mensaje: échale un vistazo y dime brevemente qué es)"
    if is_video:
        caption += (f"\n(Es un vídeo: no se ve directamente. Saca fotogramas con {BASE / '.venv/Scripts/python.exe'} "
                    f"{BASE / 'fotogramas.py'} \"{path}\", mira las imágenes y, al terminar, borra el vídeo y su "
                    f"carpeta de fotogramas con: {BASE / '.venv/Scripts/python.exe'} {BASE / 'fotogramas.py'} "
                    f"--limpiar. El usuario lo exige: no dejes vídeos ni fotogramas en el PC.)")
    enqueue(context, update.effective_chat.id, f"Te he enviado un archivo, guardado en {path}\n{caption}")


async def on_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != USER_ID:
        await query.answer()
        return
    _, approval_id, decision = query.data.split(":")
    with db() as conn:
        row = conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row or row[0] != "pending":
            await query.answer("Esta confirmación ya no está pendiente.")
            return
        conn.execute("UPDATE approvals SET status=? WHERE id=?", (decision, approval_id))
    await query.answer("Aprobado" if decision == "ok" else "Rechazado")
    # text_html conserva el formato del guardián (negritas y comando plegado); sin botones ya no se puede repetir.
    await query.edit_message_text(query.message.text_html + ("\n\n✅ APROBADO" if decision == "ok" else "\n\n❌ RECHAZADO"),
                                  parse_mode="HTML")


async def on_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != USER_ID:
        await query.answer()
        return
    _, task_id, decision = query.data.split(":")
    update_task(int(task_id), status="pendiente" if decision == "ok" else "descartada")
    await query.answer("Aceptada" if decision == "ok" else "Descartada")
    await query.edit_message_text(query.message.text + ("\n\n✅ A la cola" if decision == "ok" else "\n\n🗑️ Descartada"))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("error en handler", exc_info=context.error)


# (nombre, función, argumentos, descripción). Fuente única para los manejadores, /ayuda y el menú "/" de Telegram.
COMMANDS = [
    ("ayuda", cmd_start, "", "lista de comandos"),
    ("estado", cmd_estado, "", "qué se está haciendo ahora y con qué motor"),
    ("vercel", cmd_vercel, "", "listado actual de proyectos publicados y sus enlaces"),
    ("cupo", cmd_cupo, "", "uso de la suscripción de Claude"),
    ("motor", cmd_motor, "[auto|claude|codex|reset]", "quién trabaja (auto: Claude → Codex → gratis); reset tras cambiar de cuenta"),
    ("objetivo", cmd_objetivo, "<repo> <texto>", "Claude y Codex en paralelo sobre un mismo objetivo"),
    ("tarea", cmd_tarea, "<texto>", "añadir a la cola autónoma (las sencillas las hace un modelo gratis)"),
    ("cola", cmd_cola, "", "ver la cola de tareas"),
    ("prioridad", cmd_prioridad, "<id> <1-5>", "cambiar prioridad (5 = máxima)"),
    ("descartar", cmd_descartar, "<id>", "descartar una tarea"),
    ("reanudar", cmd_reanudar, "<id> [indicaciones]", "retomar una tarea bloqueada"),
    ("autonomia", cmd_autonomia, "[on|off]", "activar o pausar el trabajo autónomo"),
    ("latido", cmd_latido, "[2h|45m|off|ahora]", "cada cuánto trabajo solo"),
    ("resumen", cmd_resumen, "", "resumen de las últimas 24 h (sale solo a las 9:00)"),
    ("gratis", cmd_gratis, "<pregunta>", "respuesta de un modelo gratuito, sin gastar cupo"),
    ("hilo", cmd_hilo, "[nombre] [ruta]", "ver o cambiar de conversación persistente"),
    ("nuevo", cmd_nuevo, "", "empezar de cero el hilo actual"),
    ("cancelar", cmd_cancelar, "", "parar lo que se ejecuta en el hilo actual"),
    ("enviar", cmd_enviar, "<ruta>", "enviarte un archivo del PC"),
    ("recuerda", cmd_recuerda, "<texto>", "guardar algo en el cerebro"),
    ("contexto", cmd_contexto, "", "ver qué sé de ti"),
]


async def post_init(app):
    await app.bot.set_my_commands([BotCommand(name, desc[:256]) for name, _, _, desc in COMMANDS])


def main():
    INBOX.mkdir(exist_ok=True)
    db().close()
    app = Application.builder().token(ENV["TELEGRAM_TOKEN"]).post_init(post_init).build()
    me = filters.User(USER_ID)
    app.add_handler(CommandHandler(["start", "help"], cmd_start, filters=me))
    for name, fn, _, _ in COMMANDS:
        app.add_handler(CommandHandler(name, fn, filters=me))
    app.add_handler(MessageHandler(me & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(MessageHandler(me & (filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE
                                         | filters.ANIMATION), on_file))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_approval, pattern=r"^apr:"))
    app.add_handler(CallbackQueryHandler(on_proposal, pattern=r"^prop:"))
    schedule_heartbeat(app)
    app.job_queue.run_daily(daily_summary, time=datetime.time(9, 0, tzinfo=TZ), name="resumen")
    app.add_error_handler(on_error)
    log.info("bot arrancado")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
