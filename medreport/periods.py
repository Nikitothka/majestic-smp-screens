# -*- coding: utf-8 -*-
"""Рубежи сдачи отчётов и деление кадров на отчётные периоды.

Во фракции два независимых счёта. Первый — что пойдёт в СЛЕДУЮЩИЙ отчёт: это всё,
снятое после момента последней сдачи. Второй — НЕДЕЛЬНЫЙ отчёт: там считается вся
неделя целиком, включая то, что уже сдано отдельным отчётом. Поэтому кадр может
одновременно быть «уже сданным» и «идущим в недельный».

Момент сдачи отмечается вручную (кнопкой в окне), потому что знать его может только сам
игрок. Периоды получаются из списка таких моментов: всё до первого — самый старый период,
между соседними — закрытые, после последнего — открытый, ещё не сданный.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

FILE = DATA_DIR / "submissions.json"
OPEN_LABEL_FILE = DATA_DIR / "open_period.txt"
OPEN_FOLDER = "Не сдано"


def open_folder() -> str:
    """Имя папки текущего, ещё не сданного периода.

    По умолчанию «Не сдано», но его удобно назвать по тому отчёту, который готовишь —
    например «6-7». Тогда папку видно, куда выгружать, прямо по имени.
    """
    try:
        name = OPEN_LABEL_FILE.read_text(encoding="utf-8").strip()
        return name or OPEN_FOLDER
    except OSError:
        return OPEN_FOLDER


def set_open_label(name: str) -> str:
    OPEN_LABEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPEN_LABEL_FILE.write_text((name or "").strip(), encoding="utf-8")
    return open_folder()


@dataclass(frozen=True)
class Period:
    start: datetime | None      # None — от начала времён
    end: datetime | None        # None — открытый период, ещё не сдан
    label: str = ""

    @property
    def is_open(self) -> bool:
        return self.end is None

    @property
    def folder(self) -> str:
        """Имя папки периода. Двоеточие в путях Windows запрещено, поэтому час через дефис."""
        if self.is_open:
            return open_folder()
        name = f"Сдано {self.end:%d.%m %H-%M}"
        return f"{name} — {self.label}" if self.label else name

    @property
    def title(self) -> str:
        a = f"{self.start:%d.%m %H:%M}" if self.start else "с самого начала"
        b = f"{self.end:%d.%m %H:%M}" if self.end else "по сейчас"
        return f"{a} — {b}" + (f" ({self.label})" if self.label else "")

    def contains(self, t: datetime) -> bool:
        if self.start is not None and t < self.start:
            return False
        if self.end is not None and t >= self.end:
            return False
        return True


def load(path: Path = FILE) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = [d for d in data if d.get("when")]
    out.sort(key=lambda d: d["when"])
    return out


def save(items: list[dict], path: Path = FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(items, key=lambda d: d["when"]),
                               ensure_ascii=False, indent=1), encoding="utf-8")


def add(when: datetime, label: str = "", path: Path = FILE) -> list[dict]:
    """Отметить сдачу отчёта. Повторная отметка того же момента ничего не ломает."""
    items = load(path)
    stamp = when.isoformat(timespec="minutes")
    if not any(d["when"] == stamp for d in items):
        items.append({"when": stamp, "label": label})
        save(items, path)
    return items


def remove(when: str, path: Path = FILE) -> list[dict]:
    items = [d for d in load(path) if d["when"] != when]
    save(items, path)
    return items


def periods(path: Path = FILE) -> list[Period]:
    """Все периоды по порядку. Последний всегда открытый."""
    items = load(path)
    out: list[Period] = []
    prev: datetime | None = None
    for d in items:
        end = datetime.fromisoformat(d["when"])
        out.append(Period(prev, end, d.get("label", "")))
        prev = end
    out.append(Period(prev, None))
    return out


def period_of(t: datetime | str | None, path: Path = FILE) -> Period:
    """Период, которому принадлежит кадр. Без времени — считаем несданным."""
    ps = periods(path)
    if t is None:
        return ps[-1]
    if isinstance(t, str):
        try:
            t = datetime.fromisoformat(t.replace(" ", "T"))
        except ValueError:
            return ps[-1]
    for p in ps:
        if p.contains(t):
            return p
    return ps[-1]


def folder_of(t: datetime | str | None, path: Path = FILE) -> str:
    """Папка периода — или пусто, если рубежей ещё нет и делить не на что."""
    if not load(path):
        return ""
    return period_of(t, path).folder
