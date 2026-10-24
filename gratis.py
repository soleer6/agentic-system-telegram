"""Delegar una tarea de texto sencilla en un modelo gratuito (Groq/OpenRouter/Gemini).

Uso: python gratis.py "petición"   ·   echo petición | python gratis.py
Imprime la respuesta por stdout y el modelo usado por stderr. Código 2 si no hay ningún modelo libre.
Solo para texto no sensible: nada de credenciales, datos personales ni código privado completo.
"""
import sys

import free_router

prompt = " ".join(sys.argv[1:]) or sys.stdin.read()
if not prompt.strip():
    sys.exit("Uso: python gratis.py \"petición\"")
try:
    label, text = free_router.complete(prompt)
except free_router.NoFreeModel as exc:
    print(f"sin modelos gratuitos: {exc}", file=sys.stderr)
    sys.exit(2)
sys.stdout.reconfigure(encoding="utf-8")
print(text)
print(f"[gratis·{label}]", file=sys.stderr)
