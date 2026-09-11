# -*- coding: utf-8 -*-
"""OCR-движок (EasyOCR, ru+en) с кэшем результатов по хэшу файла и зоне."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .config import CACHE_DB, DATA_DIR
from .viewport import Box

_reader = None
_lock = threading.Lock()


def file_hash(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_reader():
    """Ленивая инициализация: модели грузятся один раз на процесс, пересоздаются при смене устройства."""
    global _reader
    import os
    import torch
    want_gpu = torch.cuda.is_available() and os.environ.get("MEDREPORT_DEVICE", "").lower() != "cpu"
    with _lock:
        if _reader is None or _reader[1] != want_gpu:
            import easyocr
            _reader = (easyocr.Reader(["ru", "en"], gpu=want_gpu, verbose=False), want_gpu)
        return _reader[0]


@dataclass
class Line:
    text: str
    conf: float
    box: Box   # в координатах кадра


class Cache:
    def __init__(self, db: Path = CACHE_DB):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(db, check_same_thread=False)
        self.con.execute("CREATE TABLE IF NOT EXISTS ocr (key TEXT PRIMARY KEY, val TEXT)")
        self.con.commit()

    def get(self, key: str):
        row = self.con.execute("SELECT val FROM ocr WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, val) -> None:
        self.con.execute("INSERT OR REPLACE INTO ocr(key,val) VALUES(?,?)", (key, json.dumps(val, ensure_ascii=False)))
        self.con.commit()


_cache: Cache | None = None


def cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache


def _to_gray_boost(im: Image.Image, upscale: float) -> np.ndarray:
    if upscale != 1.0:
        im = im.resize((int(im.width * upscale), int(im.height * upscale)), Image.LANCZOS)
    return np.asarray(im.convert("RGB"))


def read_zone(im: Image.Image, zone: Box, *, key: str | None = None,
              upscale: float = 1.0, min_conf: float = 0.2, paragraph: bool = False) -> list[Line]:
    """Распознать текст в зоне кадра. key — ключ кэша (хэш файла + имя зоны)."""
    if key is not None:
        hit = cache().get(key)
        if hit is not None:
            return [Line(t, c, Box(*b)) for t, c, b in hit]

    arr = _to_gray_boost(zone.crop(im), upscale)
    raw = get_reader().readtext(arr, detail=1, paragraph=paragraph)
    lines: list[Line] = []
    for item in raw:
        pts, text = item[0], item[1]
        conf = float(item[2]) if len(item) > 2 else 1.0
        if conf < min_conf:
            continue
        xs = [p[0] / upscale for p in pts]
        ys = [p[1] / upscale for p in pts]
        b = Box(int(zone.x0 + min(xs)), int(zone.y0 + min(ys)), int(zone.x0 + max(xs)), int(zone.y0 + max(ys)))
        lines.append(Line(text.strip(), conf, b))
    lines.sort(key=lambda l: (l.box.y0, l.box.x0))
    if key is not None:
        cache().put(key, [[l.text, l.conf, [l.box.x0, l.box.y0, l.box.x1, l.box.y1]] for l in lines])
    return lines


def join_rows(lines: list[Line], row_tol: int = 18) -> list[str]:
    """Склеить фрагменты, лежащие на одной строке, слева направо."""
    rows: list[list[Line]] = []
    for l in lines:
        cy = (l.box.y0 + l.box.y1) // 2
        for r in rows:
            rcy = (r[0].box.y0 + r[0].box.y1) // 2
            if abs(cy - rcy) <= row_tol:
                r.append(l)
                break
        else:
            rows.append([l])
    out = []
    for r in rows:
        r.sort(key=lambda l: l.box.x0)
        out.append(" ".join(l.text for l in r))
    return out


def release() -> None:
    """Выгрузить распознаватель из памяти видеокарты.

    Пока окно программы открыто, torch держит модели в видеопамяти, и игра из-за этого
    продолжает лагать даже после того, как разбор закончился. Поэтому по окончании работы
    модели выгружаем, а кэш видеопамяти отдаём системе.
    """
    global _reader
    with _lock:
        _reader = None
