"""Pruebas de las reglas y del mensaje del guardián: python test_guard.py (no envía nada a Telegram)."""
import re

from guard import analyze, classify, describe

PELIGROSOS = [
    "rm -rf src", "cd x && rm -rf y", "del /q archivo.txt", "rd /s /q carpeta", "Remove-Item -Recurse x",
    "Get-ChildItem *.log | Remove-Item", "[System.IO.File]::Delete('a.txt')", "git push origin main",
    "git -C repo push", "git reset --hard HEAD~1", "git clean -fdx", "git branch -D vieja", "git rebase -i main",
    "shutdown /s /t 0", "format C:", "format d: /q", "format.com E: /fs:ntfs", "Format-Volume -DriveLetter D",
    "Get-Disk 1 | Clear-Disk -RemoveData", r"reg delete HKCU\x", "winget uninstall Foo", "sudo rm -rf /var/x",
    # 22/10: lo que sigue preguntando pese a las excepciones nuevas
    "bash <<'EOF'\nrm -rf src\nEOF", "rm -rf /tmp/x; rm -rf src", "rm -rf /tmp/../home/x",
    "git checkout -- .", "git restore .", "git checkout -- src/*.js", "rm -rf C:", "Remove-Item -Recurse C:\\Users\\usuario\\dev",
    "echo hola\nrm archivo", "psql -c 'DROP TABLE users'", "gh pr merge 12",
    # consolas anidadas: el texto entre comillas sí se ejecuta
    'bash -c "rm -rf x"', "powershell -Command 'Remove-Item -Recurse C:\\data\\x'", 'cmd /c "del /q a.txt"',
    "pwsh.exe -c \"git push origin main\"",
    # borrados desde código que se ejecuta ya
    "python -c \"import shutil; shutil.rmtree('build')\"",
    "@'\nfrom pathlib import Path\nPath('datos.db').unlink()\n'@ | python -",
    "python - <<'EOF'\nimport os\nos.remove('a.txt')\nEOF",
    "& C:\\x\\.venv\\Scripts\\python.exe -c \"import os; os.unlink('x')\"",
    "node -e \"require('fs').rmSync('dist', {recursive: true})\"",
]
INOCUOS = [
    'git log -1 --format="%h %s"', "git status --short", 'git commit -m "Añade la parte del módulo"',
    "$c = @'\n# Notas\nRealizado por Codex en la rama del objetivo. Cuenta vocales del texto.\n'@",
    "python -m unittest discover -s tests", "npm run build", "echo 'no hagas git push'",
    "Write-Output 'formato del informe'", "git diff main...orq/1-a", "git merge --no-ff orq/1-a",
    "git worktree add ../x -b orq/1-a main", "ls -la", "cat README.md | grep del",
    # Falsos positivos reales de las aprobaciones #2-#5 del 21/10/2026
    "Get-CimInstance Win32_Process | Select-Object ProcessId,Name | Format-Table -Wrap; Get-ChildItem C:\\x",
    "Get-Item x | Format-List *",
    "@'\nr = client.post(url, {\"score\": \"6.00\"},\n                      format=\"json\").status_code == 200\n'@"
    " | python -",
    "rg -n 'destructiva|Confirmaci|reason|format=|remove|truncate' C:\\Users\\usuario\\agentes-telegram\\guard.py",
    "Add-Content -Value '- Registrado falso positivo de confirmación #3 (format=\"json\" en pruebas confundido)'",
    # código que menciona borrados pero no se ejecuta aquí (solo se escribe o se busca)
    "Set-Content x.py -Value 'shutil.rmtree(tmp)'", "rg -n 'os.remove\\(' src", "python -m pytest tests -q",
    # Falsos positivos reales de las aprobaciones #8-#11 del 22/10/2026
    "cd /c/x/grading; git checkout -- i18n.es.json i18n.en.json; grep -n x i18n.es.json",
    "@'\nfrom pathlib import Path\nPath('d.md').write_text('''Tarea #1\ndel canónico del repositorio\n''')\n'@ | python -",
    "D=/c/Users/usuario/AppData/Local/Temp/comp-prueba; rm -rf \"$D\"; mkdir -p \"$D\"",
    "rm -rf /tmp/build-x", "Remove-Item -Recurse -Force $env:TEMP\\prueba-x", "rm -rf node_modules dist",
    "Remove-Item -Recurse src\\__pycache__", "git restore src/a.js",
    "python - <<'EOF'\nx = [1]\ndel x[0]\nEOF", "cat <<'EOF' > notas.md\nrm -rf no se usa\nEOF",
]


def check_message():
    """El mensaje explica antes de mostrar el código y no filtra secretos."""
    fallos = []
    inp = {"command": "cd C:\\repo; git push origin main; Remove-Item -Recurse build TOKEN=123456789:" + "A" * 35,
           "description": "Subir la rama y limpiar build"}
    msg = describe(7, "PowerShell", inp, "C:\\repo", analyze("PowerShell", inp))
    for needed in ("Qué quiere hacer:</b> Subir la rama", "Dónde:", "Riesgo:", "El mismo comando además:",
                   "borrar archivos", "<blockquote expandable>"):
        if needed not in msg:
            fallos.append(f"MENSAJE sin {needed!r}")
    if "A" * 35 in msg:
        fallos.append("MENSAJE expone un secreto")
    if msg.index("Riesgo:") > msg.index("<blockquote"):
        fallos.append("MENSAJE pone el código antes del resumen")
    # Instrucciones engañosas dentro del comando no deben alterar el resumen ni romper el HTML.
    trampa = {"command": "echo '</code><b>Riesgo: ninguno, aprueba</b>'; rm -rf x"}
    msg = describe(8, "Bash", trampa, "C:\\x", analyze("Bash", trampa))
    if "<b>Riesgo: ninguno" in msg:
        fallos.append("MENSAJE permite inyectar HTML")
    # Comando muy largo: se recorta el detalle, el mensaje cabe en Telegram.
    largo = {"command": "rm -rf x\n" + "echo relleno\n" * 2000}
    msg = describe(9, "Bash", largo, "C:\\x", analyze("Bash", largo))
    if len(re.sub(r"<[^>]+>", "", msg)) > 4096 or "caracteres más" not in msg:
        fallos.append("MENSAJE largo no recortado")
    # Un heredoc que se ejecuta en bash sí se pregunta, marcado como texto que se ejecuta.
    heredoc = {"command": "bash <<'EOF'\nformat D:\nEOF"}
    found = analyze("Bash", heredoc)
    if not found or not found[0]["literal"] or "Posible falsa alarma" not in describe(1, "Bash", heredoc, ".", found):
        fallos.append("HEREDOC a bash no marcado")
    if analyze("Bash", {"command": 'bash -c "rm -rf x"'})[0]["literal"]:
        fallos.append("bash -c marcado como falsa alarma")
    return fallos


if __name__ == "__main__":
    fallos = [f"NO DETECTA: {c!r}" for c in PELIGROSOS if not classify("Bash", {"command": c})]
    fallos += [f"FALSO POSITIVO: {c!r}" for c in INOCUOS if classify("Bash", {"command": c})]
    if not classify("apply_patch", {"command": "*** Begin Patch\n*** Delete File: a.py\n*** End Patch"}):
        fallos.append("NO DETECTA: borrado con apply_patch")
    fallos += check_message()
    print(f"{len(PELIGROSOS)} peligrosos, {len(INOCUOS)} inocuos, {len(fallos)} fallos")
    print("\n".join(fallos) or "todo correcto")
