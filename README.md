# Agentes por Telegram

Controla tu PC con Windows desde el móvil a través de un bot de Telegram que trabaja con **Claude Code** y **Codex**,
con modelos gratuitos de reserva, memoria compartida y un guardián que te pide permiso antes de cualquier acción peligrosa.

## Qué hace
- **Hablas con tu bot** (texto, audios transcritos en local, fotos, archivos y vídeos) y un agente trabaja en tu PC.
- **Motores con relevo**: Claude → Codex → modelos gratis (Groq, OpenRouter, Gemini). Si uno se queda sin cupo,
  sigue el siguiente con el contexto de la conversación.
- **Guardián** (`guard.py`): borrar datos, `git push`, reescribir historial, apagar, formatear… llegan a Telegram
  con una explicación en lenguaje claro y botones Aprobar / Rechazar. Sin respuesta en 15 min, se deniega.
- **Cerebro** (`~/cerebro`): memoria compartida por Claude y Codex (perfil, proyectos, diario, notas). Así no repites contexto.
- **Modo autónomo**: cola de tareas, latido periódico, resumen diario a las 9:00; solo avisa de hitos, bloqueos y propuestas.
- **Orquestación**: `/objetivo` reparte un trabajo en subtareas en paralelo (ramas Git aisladas) con revisión cruzada.
- **Tareas simples gratis**: `/gratis` y el triaje de `/tarea` mandan lo sencillo a modelos gratuitos.

## Comandos
```
/ayuda /estado /cupo /motor /vercel
/tarea /cola /prioridad /descartar /reanudar /autonomia /latido /resumen
/objetivo <repo> <texto>   /gratis <pregunta>
/hilo /nuevo /cancelar /enviar <ruta> /recuerda <texto> /contexto
```

## Instalación
Ver **[INSTALAR.md](INSTALAR.md)**. Está escrito para que se lo des tal cual a tu agente
(Claude Code o Codex): *"Instálame este repositorio siguiendo INSTALAR.md"*.

## Seguridad
- El bot solo responde a **tu** ID de Telegram (`TELEGRAM_USER_ID`).
- Los agentes tienen acceso completo al PC: el guardián es la red de seguridad, no lo desactives.
- `.env` (token y claves) nunca se sube a Git. Si se filtra el token, revócalo en @BotFather.

## Estructura
| Archivo | Para qué |
|---|---|
| `bot.py` | El bot: hilos, motores, cola, autonomía, orquestación, comandos |
| `guard.py` | Hook de confirmaciones por Telegram (Claude y Codex) · pruebas: `test_guard.py` |
| `free_router.py`, `gratis.py` | Modelos gratuitos con relevo automático |
| `fotogramas.py` | Saca fotogramas de los vídeos recibidos (y los limpia) |
| `inventario.py` | Genera `cerebro/equipo.md` con el hardware y herramientas del PC |
| `vercel_list.py` | `/vercel`: proyectos publicados en tu cuenta de Vercel |
| `system_prompt.md`, `autonomo_prompt.md` | Instrucciones para los agentes |
| `cerebro-plantilla/` | Cerebro vacío que se copia a `~/cerebro` |
| `instalar.ps1` | Instalación completa |
