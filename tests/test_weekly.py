# -*- coding: utf-8 -*-
"""Недельный разрез: отчёт во фракции сдаётся за неделю."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.report import by_week


def row(t, points=3, folder="ПМП день (3 балла)"):
    return {"время": t, "баллы": points, "папка": folder, "статус": "разобрано"}


def test_week_starts_on_monday_and_sunday_stays_in_it():
    #  07.09.2026 — понедельник, 13.09 — воскресенье
    weeks = by_week([row("2026-09-07 10:00"), row("2026-09-13 23:00"), row("2026-09-14 10:00")])
    assert list(weeks) == ["2026-09-07", "2026-09-14"]
    assert weeks["2026-09-07"]["файлов"] == 2, "воскресенье принадлежит своей неделе"
    assert weeks["2026-09-14"]["файлов"] == 1


def test_totals_and_days():
    weeks = by_week([row("2026-09-08 17:34"), row("2026-09-08 18:03"),
                     row("2026-09-09 15:00", points=1, folder="Отменённый вызов (1 балл)")])
    w = weeks["2026-09-07"]
    assert w["баллов"] == 7 and w["файлов"] == 3
    assert w["дни"]["2026-09-08"] == {"файлов": 2, "баллов": 6}
    assert w["дни"]["2026-09-09"] == {"файлов": 1, "баллов": 1}
    assert w["по"] == "2026-09-13"


def test_unsorted_rows_are_not_counted():
    weeks = by_week([row("2026-09-08 17:34"),
                     {"время": "2026-09-08 18:00", "баллы": 0, "папка": "", "статус": "проверить"}])
    assert weeks["2026-09-07"]["файлов"] == 1
