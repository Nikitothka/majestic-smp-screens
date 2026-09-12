# -*- coding: utf-8 -*-
"""Список «сколько вписывать в форму»: суммы по папкам и разбор доп. баллов."""
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.config import load_rules
from medreport.report import folder_totals_text

R = load_rules(personal=False)
LUNCH = R.raw["lunch_folder"]


def _sample():
    cats = Counter({"ПМП день (3 балла)": 2, f"{LUNCH}/Вакцины в обед": 3})
    detail = Counter({("ПМП день (3 балла)", "pmp_day", 3): 2,
                      (f"{LUNCH}/Вакцины в обед", "vax_gkb2", 4): 2,
                      (f"{LUNCH}/Вакцины в обед", "vax_asmp", 6): 1})
    return cats, detail


def test_totals_and_lunch_breakdown():
    cats, detail = _sample()
    text = folder_totals_text(cats, detail, 20, 5, R, "ТЕСТ")
    assert "ПМП день (3 балла)" in text and " 2" in text
    # вакцина из мед.отсека скорой дороже склифовской, и обе удваиваются за обед
    assert "3 × 2 = 6 б." in text and "2 × 2 = 4 б." in text
    lines = [l for l in text.splitlines() if "ВСЕГО ДОП. БАЛЛОВ" in l]
    assert lines and lines[0].split()[-2:] == ["3", "14"], lines


def test_lunch_is_not_counted_in_main_category():
    """Обед идёт отдельной строкой: в основной категории тех же кадров быть не должно."""
    cats, detail = _sample()
    text = folder_totals_text(cats, detail, 20, 5, R, "ТЕСТ")
    main = [l for l in text.splitlines() if l.startswith("ПМП день")]
    assert len(main) == 1 and main[0].split()[-2:] == ["2", "6"]
