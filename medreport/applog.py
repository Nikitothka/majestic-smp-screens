# -*- coding: utf-8 -*-
"""Запись хода работы в файл и понижение приоритета, чтобы не мешать игре.

Лог в окне исчезает вместе с окном, а разбираться с «что-то пошло не так» приходится
потом. Поэтому всё, что программа пишет в окно, дублируется в файл на диске —
по одному файлу на день.

Приоритет процесса понижается на время работы: распознавание грузит видеокарту и
процессор, и если в это время идёт игра, она начинает лагать. С низким приоритетом
Windows отдаёт ресурсы игре первой.
"""
from __future__ import annotations

import ctypes
import sys
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
KEEP_DAYS = 14

_BELOW_NORMAL = 0x00004000
_NORMAL = 0x00000020


def log_path(day: datetime | None = None) -> Path:
    d = day or datetime.now()
    return LOG_DIR / f"{d:%Y-%m-%d}.log"


def write(text: str) -> None:
    """Дописать строку в сегодняшний лог. Ошибка записи не должна ронять разбор."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S}  {text}\n")
    except OSError:
        pass


def tee(gui_log=None):
    """Обёртка: пишет и в окно, и в файл. Возвращает функцию, принимающую (текст, тег)."""
    def _log(text: str, tag: str | None = None):
        write(str(text))
        if gui_log is not None:
            try:
                gui_log(text, tag) if tag is not None else gui_log(text)
            except TypeError:
                gui_log(text)
    return _log


def banner(what: str) -> None:
    write("=" * 60)
    write(f"{what}   {datetime.now():%Y-%m-%d %H:%M:%S}")


def cleanup(keep_days: int = KEEP_DAYS) -> None:
    """Убрать логи старше двух недель, чтобы папка не росла бесконечно."""
    if not LOG_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - keep_days * 86400
    for p in LOG_DIR.glob("*.log"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ приоритет
def _kernel32():
    """kernel32 с объявленными типами.

    Без restype=HANDLE ctypes обрезает дескриптор процесса до 32 бит, и понижение
    приоритета молча не срабатывает — именно на этом оно и не работало.
    """
    import ctypes.wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = w.HANDLE
    k.SetPriorityClass.argtypes = [w.HANDLE, w.DWORD]
    k.SetPriorityClass.restype = w.BOOL
    k.GetPriorityClass.argtypes = [w.HANDLE]
    k.GetPriorityClass.restype = w.DWORD
    return k


def _set_priority(value: int) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        k = _kernel32()
        return bool(k.SetPriorityClass(k.GetCurrentProcess(), value))
    except Exception:  # noqa: BLE001
        return False


def lower_priority() -> bool:
    """Отдать процессор игре: пока идёт разбор, программа работает фоном."""
    return _set_priority(_BELOW_NORMAL)


def normal_priority() -> bool:
    return _set_priority(_NORMAL)


def current_priority() -> str:
    if not sys.platform.startswith("win"):
        return "—"
    try:
        k = _kernel32()
        v = k.GetPriorityClass(k.GetCurrentProcess())
        return {_BELOW_NORMAL: "ниже обычного", _NORMAL: "обычный"}.get(v, hex(v))
    except Exception:  # noqa: BLE001
        return "—"


class Background:
    """На время работы — низкий приоритет, после — обратно обычный."""

    def __enter__(self):
        self.changed = lower_priority()
        return self

    def __exit__(self, *exc):
        if self.changed:
            normal_priority()
        return False
