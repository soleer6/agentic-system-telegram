"""Hook PreToolUse: las acciones peligrosas esperan aprobación por Telegram.

Claude Code lo invoca con el JSON de la herramienta por stdin. Si el comando
encaja con alguna regla, se manda un mensaje con botones Aprobar/Rechazar y se
espera a que bot.py anote la respuesta en la base de datos. Sin respuesta en
APPROVAL_TIMEOUT segundos, se deniega. La denegación va como JSON por stdout.

El mensaje empieza por un resumen en español (qué, dónde, orden detectada,
riesgo, si parece falsa alarma) y deja el comando completo como detalle
plegable. Todo se calcula aquí, sin mandar el comando a ningún modelo externo.
"""
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "data" / "bridge.db"
APPROVAL_TIMEOUT = 15 * 60

# Posición de comando: inicio de línea, tras ; & | ( { o $( (opcionalmente con sudo), o el texto que se pasa a una
# consola anidada (bash -c "...", powershell -Command '...', cmd /c "..."). Así "del" o "format" dentro de un texto
# en español o de `git log --format` no cuentan como comandos.
NESTED = r"\b(?:bash|sh|zsh|cmd|powershell|pwsh)(?:\.exe)?\s+(?:-c|/c|/k|-command)\s+[\"']?"
CMD = r"(?:^|[;&|({\n]|\$\(|" + NESTED + r")\s*(?:sudo\s+)?"
GIT = CMD + r"git\b[^;&|\n]*?\s"

BORRADO = "Borra archivos o carpetas. No pasan por la papelera: solo se recuperan si hay copia (git, OneDrive)."
# (patrón, motivo, qué supone aprobarlo). Se evalúan sin distinguir mayúsculas y en modo multilínea.
RULES = [
    (CMD + r"(rm|rmdir|rd|del|erase|ri)(\s|$)", "borrar archivos", BORRADO),
    (CMD + r"(remove-item|clear-content|clear-recyclebin|remove-itemproperty)\b", "borrar archivos o datos", BORRADO),
    (r"\[(system\.)?io\.(file|directory)\]::delete\(", "borrar archivos", BORRADO),
    (GIT + r"push\b", "subir cambios a un remoto (push)",
     "Publica commits en el servidor (GitHub…). Otros pueden verlos y deshacerlo exige otro push."),
    # checkout -- / restore de archivos concretos no cuenta (ver whole_tree_discard): solo el árbol entero.
    (GIT + r"(reset\s+--hard|clean\b|branch\s+-D\b|checkout\s+--\s|restore\b)", "descartar cambios de git",
     "Tira cambios no guardados en un commit, o ramas. Lo que no estaba en un commit se pierde."),
    (GIT + r"(rebase|filter-branch|filter-repo|stash\s+(drop|clear)|tag\s+-d)\b", "reescribir el historial de git",
     "Cambia commits ya hechos. Suele recuperarse con git reflog durante un tiempo, pero es delicado."),
    (CMD + r"gh\s+(repo\s+delete|release\s+delete|pr\s+merge)\b", "acción en GitHub",
     "Borra un repositorio o release, o fusiona un PR. Lo ven otros y es difícil de deshacer."),
    (CMD + r"(shutdown|stop-computer|restart-computer|logoff)\b", "apagar o reiniciar el equipo",
     "Cierra todo lo abierto en el PC, incluido este bot, hasta que vuelva a arrancar."),
    # format solo con una unidad detrás (format D:); Format-Table, Format-List o format="json" no son esto.
    (CMD + r"(format(\.com)?\s+[a-z]:|(diskpart|mkfs\S*|bcdedit|cipher\s+/w|reg\s+delete|format-volume|clear-disk|"
           r"initialize-disk|remove-partition)\b)", "acción de sistema destructiva",
     "Formatea o borra discos, o toca el arranque o el registro. Puede ser irreversible."),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", "borrar datos de una base de datos",
     "Elimina tablas o todo su contenido. Solo se recupera con una copia de seguridad."),
    (CMD + r"((winget|choco)\s+uninstall|uninstall-package|npm\s+unpublish)\b", "desinstalar software",
     "Quita un programa o paquete. Normalmente se puede volver a instalar."),
]

# Borrados desde código que se ejecuta en el momento (python -c, heredoc o here-string pasado a python/node).
# Solo cuentan si el comando lanza un intérprete; un archivo .py que solo se escribe no se ejecuta aquí.
INTERPRETER = re.compile(r"(?:^|[;&|({\n]|\$\()\s*(?:&\s*)?\S*\b(?:python3?|py|node)(?:\.exe)?['\"]?(?:\s|$)",
                         re.IGNORECASE | re.MULTILINE)
CODE_RULES = [
    (r"\b(shutil\.rmtree|os\.(remove|unlink|rmdir|removedirs)|send2trash)\s*\(|\.(unlink|rmdir)\(\s*(missing_ok\s*=[^)]*)?\)",
     "borrar archivos desde código Python", BORRADO),
    (r"\.(rm|rmdir|unlink)Sync\s*\(|\bfs(\.promises)?\.(rm|rmdir|unlink)\s*\(", "borrar archivos desde código Node",
     BORRADO),
]

SECRETS = [
    (re.compile(r"(?i)\b(token|password|passwd|pwd|secret|api[_-]?key)(\s*[=:]\s*)(['\"]?)[^\s'\";&|]+"),
     r"\1\2\3•••"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "•••"),  # token de bot de Telegram
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})"),
     "•••"),
]


def env():
    values = {}
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


# Texto que sí se ejecuta como consola: bash <<EOF, ... | bash, | iex. Lo demás entre comillas o en un heredoc es
# texto (un documento, código Python donde "del" es una palabra clave...) y no se pregunta por ello.
SHELL_FEED = re.compile(r"\b(bash|sh|zsh|pwsh|powershell|cmd)(\.exe)?\s+(-s\s+)?<<|\|\s*(bash|sh|zsh|pwsh|powershell|"
                        r"iex|invoke-expression|cmd)\b", re.IGNORECASE)

# Borrar aquí no es un riesgo: temporales y artefactos que se regeneran solos.
SAFE_DIRS = re.compile(r"(^|[\\/])(tmp|temp)([\\/]|$)|appdata[\\/]local[\\/]temp|^\$(env:)?(tmp|temp)\b|^\$\{?(tmp|temp)dir",
                       re.IGNORECASE)
ARTIFACTS = {"node_modules", "dist", "build", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".vite",
             "coverage", "htmlcov", ".next", ".turbo", ".cache"}
DELETE_CMD = re.compile(r"^(?:sudo\s+)?(rm|rmdir|rd|del|erase|ri|remove-item)\b", re.IGNORECASE)


def assignments(command):
    """$x = 'ruta' (PowerShell) y x=ruta / x="ruta" (bash), para resolver variables en la orden detectada."""
    found = dict(re.findall(r"\$(\w+)\s*=\s*['\"]([^'\"\n]+)['\"]", command))
    found.update((k, v) for k, _, v in re.findall(r"(?:^|[;&\s])(\w+)=(['\"]?)([^\s'\";&|$`(]+)\2", command))
    return found


def harmless_delete(segment):
    """True si la orden de borrado solo apunta a temporales o artefactos regenerables (rm -rf $TMP/x, dist...)."""
    m = DELETE_CMD.match(segment)
    if not m:
        return False
    targets = [t.strip("'\"") for t in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", segment[m.end():])]
    targets = [t for t in targets if t and not t.startswith("-") and not re.fullmatch(r"/[a-z]", t, re.I)]
    if not targets:
        return False
    for t in targets:
        norm = t.replace("\\", "/").rstrip("/")
        name = norm.rsplit("/", 1)[-1]
        if norm in ("", "/", "~", "*", ".") or ".." in norm or re.fullmatch(r"[a-z]:", norm, re.I):
            return False
        if not (SAFE_DIRS.search(t) or name in ARTIFACTS or name.endswith(".pyc")):
            return False
    return True


def whole_tree_discard(segment):
    """git checkout -- / git restore de archivos concretos: deshace solo esos archivos, no se pregunta."""
    m = re.search(r"\b(checkout\s+--|restore)\s+(.*)$", segment)
    if not m or re.search(r"\b(reset|clean|branch)\b", segment):
        return True
    paths = [p for p in m.group(2).split() if not p.startswith("-")]
    return not paths or any(p in (".", "*", ":/", "./") or "*" in p for p in paths)


def literal_spans(command):
    """Tramos que son texto y no órdenes: comillas, here-strings de PowerShell (@' '@) y heredocs (<<EOF)."""
    spans, i, n = [], 0, len(command)
    heredoc = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
    while i < n:
        m = heredoc.match(command, i)
        if m:
            start = command.find("\n", m.end())
            if start < 0:
                break
            end = re.compile(rf"^\s*{re.escape(m.group(1))}\s*$", re.M).search(command, start + 1)
            spans.append((start, end.start() if end else n))
            i = end.end() if end else n
            continue
        if command.startswith(("@'", '@"'), i):
            close = command.find("\n" + command[i + 1] + "@", i + 2)
            stop = close if close >= 0 else n
            spans.append((i, stop))
            i = stop + 3
            continue
        if command[i] in "'\"":
            quote, j = command[i], i + 1
            while j < n and command[j] != quote:
                j += 2 if quote == '"' and command[j] in "\\`" else 1
            spans.append((i, j))
            i = j + 1
            continue
        i += 1
    return spans


def analyze(tool, tool_input):
    """Coincidencias del comando: dicts con motivo, riesgo, orden detectada y si cae dentro de un texto."""
    command = tool_input.get("command", "")
    if tool == "apply_patch":  # Codex también puede borrar archivos con un parche
        files = re.findall(r"^\*\*\* Delete File:\s*(.+)$", command, re.M)
        return [{"reason": "borrar archivos", "risk": BORRADO, "segment": f"borrar {f.strip()}", "literal": False}
                for f in files]
    if tool not in ("Bash", "PowerShell"):
        return []
    spans = literal_spans(command)
    # $carpeta = 'C:\...' → la orden detectada muestra la ruta real en vez de la variable.
    variables = assignments(command)
    feeds_shell = bool(SHELL_FEED.search(command))
    findings = []
    for pattern, reason, risk in RULES:
        # Todas las apariciones: que el primer rm sea inocuo no debe tapar un segundo rm peligroso.
        for m in re.finditer(pattern, command, re.IGNORECASE | re.MULTILINE):
            # Salta el separador (; | salto de línea, bash -c ") para quedarse con la orden en sí.
            pos = m.start() + len(m.group(0)) - len(m.group(0).lstrip(";&|({\n\t $"))
            nested = re.match(NESTED, command[pos:], re.IGNORECASE)
            if nested:
                pos += nested.end()  # el texto de bash -c "..." sí se ejecuta: no es falsa alarma
            segment = re.split(r"[;&|\n]", command[pos:], maxsplit=1)[0].strip()
            segment = re.sub(r"\$\{?(\w+)\}?", lambda v: variables.get(v.group(1), v.group(0)), segment)
            literal = not nested and any(a <= pos < b for a, b in spans)
            if literal and not feeds_shell and pattern.startswith(CMD):  # SQL (DROP TABLE) va siempre en texto
                continue  # texto (un documento, código Python donde "del" es palabra clave...), no una orden
            if risk == BORRADO and harmless_delete(segment):
                continue  # temporales o artefactos que se regeneran
            if reason == "descartar cambios de git" and not whole_tree_discard(segment):
                continue  # deshace archivos concretos, no el repositorio entero
            findings.append({"reason": reason, "risk": risk, "segment": segment[:160], "literal": literal})
            break
    if INTERPRETER.search(command):
        for pattern, reason, risk in CODE_RULES:
            m = re.search(pattern, command)
            if m:
                line = command[command.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
                findings.append({"reason": reason, "risk": risk, "segment": line[:160], "literal": False})
    return findings


def classify(tool, tool_input):
    findings = analyze(tool, tool_input)
    return findings[0]["reason"] if findings else None


def redact(text):
    for pattern, repl in SECRETS:
        text = pattern.sub(repl, text)
    return text


def describe(approval_id, tool, tool_input, cwd, findings):
    """Mensaje HTML de Telegram: resumen comprensible primero y el comando completo plegado al final."""
    def e(s):
        return html.escape(redact(str(s)), quote=False)

    main = next((f for f in findings if not f["literal"]), findings[0])
    what = tool_input.get("description") or tool_input.get("justification")
    lines = [f"🔐 <b>Autorización #{approval_id}: {e(main['reason'])}</b>", ""]
    lines.append(f"<b>Qué quiere hacer:</b> {e(what[:400])}" if what else
                 "<b>Qué quiere hacer:</b> el agente no lo ha explicado; fíjate en la orden detectada.")
    lines.append(f"<b>Dónde:</b> <code>{e(cwd)}</code>")
    lines.append(f"<b>Orden detectada:</b> <code>{e(main['segment'])}</code>")
    lines.append(f"<b>Riesgo:</b> {e(main['risk'])}")
    others = [f for f in findings if f is not main]
    if others:
        lines.append("<b>El mismo comando además:</b>")
        lines += [f"• {e(f['reason'])}{' (dentro de un texto)' if f['literal'] else ''}: <code>{e(f['segment'])}</code>"
                  for f in others]
    if all(f["literal"] for f in findings):
        lines += ["", "⚠️ <b>Posible falsa alarma:</b> la orden aparece dentro de un texto o bloque de código, no "
                      "como orden directa de la consola. Solo sería real si ese texto se ejecuta (por ejemplo un "
                      "script que se pasa a python o a bash -c). Es una sospecha por la forma del comando, no un "
                      "análisis completo."]
    command = tool_input.get("command", "")
    cut = f"\n… ({len(command) - 2500} caracteres más, guardados en el PC)" if len(command) > 2500 else ""
    lines += ["", f"<b>Comando completo</b> ({tool}, toca para desplegar):",
              f"<blockquote expandable>{e(command[:2500])}{e(cut)}</blockquote>",
              f"Caduca en {APPROVAL_TIMEOUT // 60} min · sin respuesta = rechazado."]
    return "\n".join(lines)


def deny(message):
    """Deniega por JSON en stdout: funciona en Claude y en Codex, que en Windows pierde el código de salida 2."""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": message}}))
    print(message, file=sys.stderr)
    sys.exit(0)


def telegram(token, method, payload):
    data = urllib.parse.urlencode(payload).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=20) as r:
        return json.load(r)


def main():
    # Codex y Claude envían UTF-8; la consola de Windows leería Windows-1252.
    event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    if os.environ.get("CLAUDE_BRIDGE") != "1":
        sys.exit(0)  # sesiones normales de Codex/Claude: ya tienen sus propias confirmaciones
    tool, tool_input = event.get("tool_name", ""), event.get("tool_input", {})
    findings = analyze(tool, tool_input)
    if not findings:
        sys.exit(0)

    command = tool_input.get("command", "")
    reason = findings[0]["reason"]
    # El directorio real del comando: Codex lo indica en workdir; si no, el del agente.
    cwd = tool_input.get("workdir") or event.get("cwd") or "?"
    if os.environ.get("GUARD_TEST_LOG"):
        # Modo prueba: deja constancia (incluido el mensaje que se habría enviado) y deniega sin molestar al usuario.
        with open(os.environ["GUARD_TEST_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"tool": tool, "command": command, "reason": reason,
                                 "message": describe(0, tool, tool_input, cwd, findings)}, ensure_ascii=False) + "\n")
        deny("Bloqueado por el guardián (modo prueba). No lo reintentes por otra vía.")
    cfg = env()
    db = sqlite3.connect(DB, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY, created REAL, tool TEXT, "
               "detail TEXT, reason TEXT, status TEXT)")
    cur = db.execute("INSERT INTO approvals (created, tool, detail, reason, status) VALUES (?,?,?,?, 'pending')",
                     (time.time(), tool, command, reason))
    db.commit()
    approval_id = cur.lastrowid

    keyboard = {"inline_keyboard": [[{"text": "✅ Aprobar", "callback_data": f"apr:{approval_id}:ok"},
                                     {"text": "❌ Rechazar", "callback_data": f"apr:{approval_id}:no"}]]}
    payload = {"chat_id": cfg["TELEGRAM_USER_ID"], "text": describe(approval_id, tool, tool_input, cwd, findings),
               "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
    try:
        try:
            sent = telegram(cfg["TELEGRAM_TOKEN"], "sendMessage", payload)
        except urllib.error.HTTPError:  # HTML rechazado: el mismo texto sin formato
            payload.pop("parse_mode")
            payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))
            sent = telegram(cfg["TELEGRAM_TOKEN"], "sendMessage", payload)
    except Exception as exc:
        deny(f"No se pudo pedir confirmación por Telegram ({exc}); acción bloqueada.")

    deadline = time.time() + APPROVAL_TIMEOUT
    while time.time() < deadline:
        status = db.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[0]
        if status == "ok":
            sys.exit(0)
        if status == "no":
            deny("El usuario ha RECHAZADO esta acción por Telegram. No la reintentes ni la rodees; "
                 "pregúntale qué prefiere.")
        time.sleep(2)

    db.execute("UPDATE approvals SET status='timeout' WHERE id=?", (approval_id,))
    db.commit()
    try:  # quita los botones para que no se pulse una autorización que ya no sirve
        telegram(cfg["TELEGRAM_TOKEN"], "editMessageReplyMarkup",
                 {"chat_id": cfg["TELEGRAM_USER_ID"], "message_id": sent["result"]["message_id"],
                  "reply_markup": json.dumps({"inline_keyboard": [[{"text": "⌛ Caducada: rechazada",
                                                                    "callback_data": f"apr:{approval_id}:no"}]]})})
    except Exception:
        pass
    deny("El usuario no confirmó la acción a tiempo; queda bloqueada. Avísale y sigue con otra cosa.")


if __name__ == "__main__":
    main()
