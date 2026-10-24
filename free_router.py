"""Router de modelos gratuitos para tareas simples (sin herramientas).

Prueba los modelos en orden y salta al siguiente ante cualquier fallo (cupo,
saturación, timeout). Un modelo que falla queda en pausa COOLDOWN segundos.

Garantía de coste: solo se usan cuentas en plan gratuito (comprobado por el
usuario: sin método de pago) y en OpenRouter solo modelos ":free"; cualquier
otro se rechaza antes de enviar nada.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
COOLDOWN = 10 * 60
TIMEOUT = 45
UA = "claude-bridge/1.0"  # Groq rechaza el agente por defecto de urllib

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
}
# Orden de preferencia: (proveedor, modelo). Medido 2026-10-21: Groq < 1 s, OpenRouter ~1,5 s,
# Gemini 38-45 s (por eso va al final).
MODELS = [
    ("groq", "openai/gpt-oss-120b"),
    ("groq", "qwen/qwen3.8-27b"),
    ("openrouter", "minimax/minimax-m3.1-flash:free"),
    ("openrouter", "google/gemma-4-31b-it:free"),
    ("gemini", "gemini-flash-lite-latest"),
    ("gemini", "gemini-flash-latest"),
]

_cooldown: dict[tuple, float] = {}


class NoFreeModel(Exception):
    pass


def _keys():
    values = {}
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def _call(provider, model, messages, key):
    if provider == "openrouter" and not model.endswith(":free"):
        raise ValueError(f"{model} no es un modelo gratuito de OpenRouter")
    url, _ = PROVIDERS[provider]
    body = json.dumps({"model": model, "messages": messages}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    text = (data["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise ValueError("respuesta vacía")
    return text


def complete(prompt, system=None):
    """Devuelve (etiqueta 'proveedor/modelo', texto). Lanza NoFreeModel si ninguno responde."""
    keys = _keys()
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    errors = []
    for provider, model in MODELS:
        key = keys.get(PROVIDERS[provider][1])
        if not key or _cooldown.get((provider, model), 0) > time.time():
            continue
        try:
            return f"{provider}/{model.split('/')[-1]}", _call(provider, model, messages, key)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as exc:
            detail = exc.code if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            errors.append(f"{provider}/{model}: {detail}")
            _cooldown[(provider, model)] = time.time() + COOLDOWN
    raise NoFreeModel("; ".join(errors) or "todos los modelos gratuitos están en pausa")


if __name__ == "__main__":
    print(complete("Responde solo: ok"))
