"""Inventario de produccion en vivo, usando la sesion local de Vercel CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import urlsplit


class VercelListError(RuntimeError):
    pass


def cli_command():
    found = shutil.which("vercel.cmd") or shutil.which("vercel")
    npm = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "npm"
    candidates = [npm / "node_modules/vercel/dist/index.js"]
    if found:
        candidates.insert(0, Path(found).parent / "node_modules/vercel/dist/index.js")
    for entry in candidates:
        if entry.is_file():
            return [shutil.which("node") or "node", str(entry)]
    if found:
        return [found]
    raise VercelListError("No encuentro Vercel CLI en el PC.")


def fetch_projects(scope=None):
    command = cli_command()
    projects, seen, cursor = [], set(), None
    deadline = time.monotonic() + 60
    context = scope or "cuenta actual"
    while True:
        args = command + ["project", "ls", "--json", "--limit", "100", "--non-interactive"]
        if scope:
            args += ["--scope", scope]
        if cursor is not None:
            args += ["--next", str(cursor)]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VercelListError("Vercel está tardando demasiado. Prueba /vercel en un momento.")
        try:
            result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    cwd=Path.home(), timeout=min(30, remaining),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise VercelListError("Vercel está tardando demasiado. Prueba /vercel en un momento.") from None
        except OSError:
            raise VercelListError("No he podido iniciar Vercel CLI en el PC.") from None
        if result.returncode:
            err = result.stderr.lower()
            if any(word in err for word in ("login", "unauthorized", "authentication", "token")):
                raise VercelListError("La sesión de Vercel ha caducado. Hay que volver a iniciar sesión en el PC.")
            raise VercelListError("No he podido consultar Vercel ahora. Prueba de nuevo en un momento.")
        try:
            # Algunas versiones de la CLI anteponen una pista del plugin a stdout.
            start = result.stdout.index("{")
            data = json.loads(result.stdout[start:])
            page = data["projects"]
            if not isinstance(page, list):
                raise ValueError("projects")
        except (ValueError, KeyError, TypeError):
            raise VercelListError("Vercel ha devuelto un listado que no he podido interpretar.") from None
        projects.extend(page)
        context = data.get("contextName") or context
        cursor = (data.get("pagination") or {}).get("next")
        if cursor is None:
            break
        if cursor in seen:
            raise VercelListError("Vercel no ha completado el listado. Vuelve a probar /vercel.")
        seen.add(cursor)
    return context, projects


def format_projects(context, projects):
    published, unpublished, seen = [], 0, set()
    for project in projects:
        key = project.get("id") or project.get("name")
        if key in seen:
            continue
        seen.add(key)
        raw = project.get("latestProductionUrl")
        if not raw:
            unpublished += 1
            continue
        url = raw if raw.startswith("https://") else "https://" + raw
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            continue
        # Solo direcciones de la app; nunca enlaces privados de panel con claves.
        url = f"https://{parsed.netloc}{parsed.path}".rstrip("/")
        published.append((str(project.get("name") or "Proyecto"), url))
    published.sort(key=lambda item: item[0].casefold())
    header = f"▲ Vercel · {context}\n{len(published)} proyectos publicados en producción."
    lines = [header]
    for name, url in published:
        lines.append(f"{name}\n{url}")
    if not published:
        lines.append("No hay proyectos con despliegue de producción.")
    if unpublished:
        lines.append(f"Además, {unpublished} proyecto(s) sin publicación de producción.")
    lines.append("Consultado ahora en Vercel · un enlace por proyecto.")
    # Bloques completos para no cortar un enlace entre mensajes de Telegram.
    chunks, current = [], ""
    for line in lines:
        joined = f"{current}\n\n{line}" if current else line
        if len(joined) > 3900:
            chunks.append(current)
            current = line
        else:
            current = joined
    if current:
        chunks.append(current)
    return chunks


def list_messages(scope=None):
    return format_projects(*fetch_projects(scope))
