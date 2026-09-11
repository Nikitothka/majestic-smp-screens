# -*- coding: utf-8 -*-
"""Прогон извлечения по папкам-источникам. Результат — JSON на каждый файл в data/scan/."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import DEFAULT_SOURCES, DATA_DIR, IMAGE_SUFFIXES, SERVICE_DIRS
from .extract import extract

SCAN_DIR = DATA_DIR / "scan"


def iter_images(sources: list[Path]):
    skip = set(SERVICE_DIRS.values())
    for src in sources:
        if not src.exists():
            continue
        for p in sorted(src.rglob("*")):
            if p.suffix.lower() in IMAGE_SUFFIXES and not (set(p.parts) & skip):
                yield p


def scan(sources: list[Path] | None = None, *, log=print) -> list[dict]:
    sources = sources or DEFAULT_SOURCES
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    files = list(iter_images(sources))
    t0 = time.time()
    for i, p in enumerate(files, 1):
        t = time.time()
        try:
            f = extract(p).to_dict()
        except Exception as e:  # noqa: BLE001
            f = {"path": str(p), "problems": [f"ошибка: {e!r}"]}
        out.append(f)
        (SCAN_DIR / (f.get("hash") or f"err{i}")).with_suffix(".json").write_text(
            json.dumps(f, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"[{i}/{len(files)}] {time.time()-t:5.1f}s  {f.get('action') or '-':14} #{f.get('patient_id') or '-':6} "
            f"{(f.get('time') or '')[:16]:16} {p.name[:50]}")
    log(f"готово: {len(files)} файлов за {time.time()-t0:.0f}s")
    (DATA_DIR / "scan_all.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    srcs = [Path(a) for a in sys.argv[1:]] or None
    scan(srcs)
