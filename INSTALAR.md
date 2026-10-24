# Instalación (guía para el agente que lo instala)

Eres Claude Code o Codex instalando este puente en el PC con Windows de tu usuario. Sigue los pasos en orden,
explícale cada cosa que necesite hacer él y no continúes hasta que la haya hecho.

## 1. Requisitos (compruébalos y, si falta algo, ayúdale a instalarlo)
- Windows 10/11, **Python 3.12**, **Git**.
- **Claude Code** con sesión iniciada (`claude` en la terminal) y/o **Codex** con `codex login`.
  Con uno basta; con los dos tiene relevo cuando uno se queda sin cupo.
- Opcional: Vercel CLI (`npm i -g vercel`) para el comando `/vercel`.

## 2. Bot de Telegram (lo hace el usuario en el móvil)
1. Abrir **@BotFather** → `/newbot` → elegir nombre y usuario (acaba en `bot`). Guardar el **token**.
2. Abrir **@userinfobot** → copiar su **ID numérico**.
3. Recomendado en @BotFather: `/setprivacy` → Enable, y no añadir el bot a grupos.

## 3. Claves opcionales de modelos gratuitos (sin tarjeta)
- Groq: https://console.groq.com/keys
- OpenRouter: https://openrouter.ai/keys (usa solo modelos `:free`; no añadir saldo)
- Gemini: https://aistudio.google.com/apikey

## 4. Configurar
En la carpeta del repositorio: copiar `.env.example` a `.env` y rellenar `TELEGRAM_TOKEN`, `TELEGRAM_USER_ID`
y las claves opcionales. **No pegues el token en ningún sitio más ni lo subas a Git.**

## 5. Instalar
```powershell
powershell -ExecutionPolicy Bypass -File instalar.ps1 -BotName "@NombreDeSuBot"
```
Esto copia el puente a `~/agentes-telegram`, crea el entorno de Python, configura el guardián para Claude
(`guard_settings.json`) y para Codex (`~/.codex/hooks.json`, con copia de seguridad del anterior), crea `~/cerebro`
desde la plantilla, enlaza el cerebro en `~/.claude/CLAUDE.md` y `~/.codex/AGENTS.md`, programa el arranque
con Windows y ejecuta las pruebas del guardián (deben salir **0 fallos**).

## 6. Arrancar y probar
1. Arrancar: `wscript "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\agentes-telegram.vbs"`
2. En Telegram, escribir al bot `/ayuda` y luego un mensaje normal ("¿qué hay en mi escritorio?").
3. Probar el guardián: pedir "crea una carpeta de prueba en el escritorio y bórrala". Debe llegar una
   **Autorización** con botones. Rechazar → el agente no la borra.
4. `/estado` y `/cupo` deben responder.

## 7. Personalizar
- Rellenar `~/cerebro/perfil.md` con el usuario (idioma, tono, cuánta autonomía quiere).
- Opcional: `.venv\Scripts\python.exe inventario.py` para generar `~/cerebro/equipo.md`.

## Problemas típicos
- **El bot no contesta**: revisar `data/bot.log`; comprobar token e ID en `.env`; que solo haya un proceso `bot.py`.
- **Codex borra sin preguntar**: `~/.codex/hooks.json` no apunta a `guard.py`; repetir el paso 5.
- **Reiniciar el bot** (p. ej. tras cambiar `bot.py`): ejecutar `reiniciar_bot.ps1` desde un proceso independiente;
  `start_bridge.cmd` lo relanza en 10 s.
- **Vídeos > 20 MB**: límite de la API de bots de Telegram; mandarlos más cortos o por enlace.
