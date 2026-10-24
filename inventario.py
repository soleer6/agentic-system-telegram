"""Regenera cerebro/equipo.md con el inventario del PC."""
import datetime
import json
import shutil
import subprocess
from pathlib import Path

CEREBRO = Path.home() / "cerebro"
TOOLS = {
    "claude": ["claude", "--version"],
    "codex": ["codex", "--version"],
    "node": ["node", "--version"],
    "python": ["python", "--version"],
    "git": ["git", "--version"],
    "docker": ["docker", "--version"],
    "gh": ["gh", "--version"],
}


def run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, shell=(cmd[0] != "powershell"))
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else ""
    except Exception:
        return ""


def powershell_json(script):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script + " | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.stdout.strip() else None
    except Exception:
        return None


def main():
    lines = [f"# Equipo (generado {datetime.datetime.now():%Y-%m-%d %H:%M}, no editar a mano)", ""]

    info = powershell_json(
        "$os=Get-CimInstance Win32_OperatingSystem; $cpu=Get-CimInstance Win32_Processor | Select-Object -First 1; "
        "[pscustomobject]@{os=$os.Caption; build=$os.BuildNumber; cpu=$cpu.Name; cores=$cpu.NumberOfLogicalProcessors; "
        "ram=[math]::Round($os.TotalVisibleMemorySize/1MB,1); host=$env:COMPUTERNAME; user=$env:USERNAME}")
    if info:
        lines += [f"- Host: {info['host']} (usuario {info['user']})",
                  f"- SO: {info['os']} build {info['build']}",
                  f"- CPU: {info['cpu']} ({info['cores']} hilos)",
                  f"- RAM: {info['ram']} GB"]
    gpus = powershell_json("Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name")
    if gpus:
        lines.append(f"- GPU: {', '.join(gpus if isinstance(gpus, list) else [gpus])}")
    disks = powershell_json("Get-PSDrive -PSProvider FileSystem | Where-Object Used | "
                            "Select-Object Name,@{n='libre';e={[math]::Round($_.Free/1GB)}},@{n='total';e={[math]::Round(($_.Used+$_.Free)/1GB)}}")
    if disks:
        for d in disks if isinstance(disks, list) else [disks]:
            lines.append(f"- Disco {d['Name']}: {d['libre']} GB libres de {d['total']} GB")

    lines += ["", "## Herramientas"]
    for name, cmd in TOOLS.items():
        lines.append(f"- {name}: {run(cmd) if shutil.which(cmd[0]) else 'no instalado'}")

    lines += ["", "## Rutas clave",
              f"- Home: {Path.home()}",
              f"- Proyectos: {Path.home() / 'dev'}",
              f"- Cerebro: {CEREBRO}",
              f"- Puente Telegram: {Path.home() / 'claude-bridge'}"]
    dev = Path.home() / "dev"
    if dev.exists():
        lines += ["", "## Repos en ~/dev"]
        for p in sorted(dev.iterdir()):
            if (p / ".git").exists():
                branch = run(["git", "-C", str(p), "branch", "--show-current"])
                lines.append(f"- {p.name} ({branch or '?'})")

    (CEREBRO / "equipo.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
