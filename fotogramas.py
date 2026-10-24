"""Fotogramas de un vídeo para poder "verlo", sin llenar el disco.

  python fotogramas.py vídeo.mp4 [n]   saca n imágenes (por defecto 12, máximo 30) repartidas por el vídeo
  python fotogramas.py --limpiar        borra todos los vídeos recibidos y sus fotogramas

Todo vive en %TEMP%\\claude-bridge-videos. Además, cada ejecución borra lo que tenga más de 6 horas,
por si alguna vez no se limpió. Usa PyAV + Pillow, sin ffmpeg.
"""
import os
import shutil
import sys
import time
from pathlib import Path

DIR = Path(os.environ.get("TEMP", Path(__file__).parent)) / "claude-bridge-videos"


def clean(max_age=0):
    """Borra vídeos y carpetas de fotogramas con más de max_age segundos (0 = todo). Devuelve MB liberados."""
    freed = 0
    if not DIR.exists():
        return 0
    for item in DIR.iterdir():
        if time.time() - item.stat().st_mtime < max_age:
            continue
        if item.is_dir():
            freed += sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
            shutil.rmtree(item, ignore_errors=True)
        else:
            freed += item.stat().st_size
            item.unlink(missing_ok=True)
    return freed / 1e6


def extract(video, n):
    import av  # solo hace falta al extraer

    out = DIR / (video.stem + "_fotogramas")
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        duration = float(container.duration / av.time_base) if container.duration else 0
        targets = [duration * (i + 0.5) / n for i in range(n)] if duration else [0.0]
        i = 0
        for frame in container.decode(stream):
            t = float(frame.time or 0)
            if t >= targets[i]:
                img = frame.to_image()
                img.thumbnail((900, 900))
                path = out / f"{len(saved) + 1:02d}_{t:05.1f}s.jpg"
                img.save(path, quality=75)
                saved.append(path)
                while i < len(targets) and t >= targets[i]:
                    i += 1
                if i >= len(targets):
                    break
    print(f"duración {duration:.1f}s · {len(saved)} fotogramas en {out}")
    print("\n".join(str(p) for p in saved))


if __name__ == "__main__":
    if sys.argv[1:] == ["--limpiar"]:
        print(f"limpio: {clean():.1f} MB liberados en {DIR}")
    else:
        old = clean(max_age=6 * 3600)
        if old:
            print(f"(borrados restos antiguos: {old:.1f} MB)")
        extract(Path(sys.argv[1]), min(30, int(sys.argv[2]) if len(sys.argv) > 2 else 12))
