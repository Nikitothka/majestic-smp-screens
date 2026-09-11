# -*- coding: utf-8 -*-
"""Недельный отчёт отдельным файлом: считает всю неделю, включая уже сданное."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.report import by_week, week_report_text
from medreport.config import load_rules

R = load_rules(personal=False)            # тесты не зависят от личных настроек машины
R.raw["promotions"]["target"] = "6-7"
R.raw["promotions"]["spare"] = {"vaccines": 1}


def row(t, points=3, folder="ПМП день (3 балла)", act="реанимация", pid="111"):
    lunch = folder.startswith("Доп. баллы")
    cat = next((c["id"] for c in R.categories if folder.endswith(c["folder"])), "pmp_day")
    return {"время": t, "баллы": points, "папка": folder, "статус": "разобрано",
            "категория": cat, "действие": act, "ID пациента": pid, "место": "улица",
            "обед": "да" if lunch else ""}


def test_week_file_counts_everything_in_the_week():
    rows = [row("2026-09-08 17:00"), row("2026-09-09 18:00"),
            row("2026-09-09 14:30", points=6, folder="Доп. баллы (обед 14-15)/ПМП день (3 балла)"),
            row("2026-09-14 10:00")]           # следующая неделя — не считается
    weeks = by_week(rows)
    txt = week_report_text(weeks["2026-09-07"], rows, R, "тест")
    assert "07.09.2026 — 13.09.2026" in txt
    assert "ВСЕГО ЗА НЕДЕЛЮ: 12 баллов, 3 скриншота" in txt
    assert "ПМП: 1 шт = 6 б." in txt, "обеденный ПМП — в разделе доп. баллов"
    assert "14.09" not in txt, "чужая неделя не должна попасть"


def test_week_file_lists_days():
    rows = [row("2026-09-08 17:00"), row("2026-09-08 18:00"), row("2026-09-09 12:00")]
    txt = week_report_text(by_week(rows)["2026-09-07"], rows, R, "тест")
    assert "вт 08.09" in txt and "ср 09.09" in txt


def test_submitted_screenshots_still_count_for_the_week():
    """Кадр, ушедший в отдельный отчёт, из недельного не выпадает."""
    rows = [row("2026-09-08 17:00"), row("2026-09-09 22:00")]
    txt = week_report_text(by_week(rows)["2026-09-07"], rows, R, "тест")
    assert "ВСЕГО ЗА НЕДЕЛЮ: 6 баллов, 2 скриншота" in txt


# ------------------------------------------- сданное не идёт в следующее повышение
def test_promotion_ignores_already_submitted_screenshots():
    """Кадры, ушедшие в сданный отчёт, в зачёт следующего повышения не идут.

    Иначе одна и та же работа засчиталась бы дважды: сперва на повышение 5→6,
    потом ещё раз на 6→7.
    """
    from medreport.report import promotion_progress
    from medreport.score import Decision
    from medreport import periods

    def shot(period):
        return ({"период": period},
                Decision(bucket="category", category_id="pmp_day",
                         folder="ПМП день (3 балла)", points=3))

    items = ([shot("Сдано 09.09 21-44") for _ in range(100)]
             + [shot(periods.open_folder()) for _ in range(3)])
    prog = promotion_progress(items, R, swaps=[])
    assert prog["counted"] == 3, "считаем только несданные"
    assert prog["have"]["pmp"] == 3
    assert prog["have"]["points"] == 9


def test_promotion_counts_everything_when_nothing_submitted_yet():
    from medreport.report import promotion_progress
    from medreport.score import Decision
    items = [({"период": ""}, Decision(bucket="category", category_id="pmp_day",
                                       folder="ПМП день (3 балла)", points=3)) for _ in range(5)]
    prog = promotion_progress(items, R, swaps=[])
    assert prog["counted"] == 5 and prog["have"]["pmp"] == 5


def test_swapped_screenshots_count_as_real_work_in_weekly():
    """Замена — механика отчёта на повышение. В недельном считается сделанная работа:
    отданный в замену ПМП идёт как ПМП по своей цене, а не пропадает."""
    rows = [row("2026-09-10 12:00"),
            dict(row("2026-09-10 12:01"), категория="", баллы=0, папка="Замены/10 ПМП → 2 вакцины",
                 замена=True, **{"исходная категория": "pmp_day", "исходные баллы": 3})]
    txt = week_report_text(by_week(rows)["2026-09-07"], rows, R, "тест")
    assert "ПМП день (3 балла): 2 шт = 6 б." in txt
    assert "ВСЕГО ЗА НЕДЕЛЮ: 6 баллов, 2 скриншота" in txt


def test_swap_from_submitted_period_does_not_count_for_next_promotion():
    """Замену сделали для отчёта 6-7 — после его сдачи она не должна
    добавлять вакцины к следующему повышению."""
    from medreport.report import promotion_progress
    from medreport.score import Decision
    from medreport import periods
    sw = [{"title": "10 ПМП → 2 вакцины", "get": 2, "get_what": "vaccinate", "gain": 6}]
    old_period = [({"период": "Сдано 11.09 13-40 — 6-7"},
                   Decision(bucket="category", category_id=None,
                            folder="Замены/10 ПМП → 2 вакцины", points=0))]
    now = [({"период": periods.open_folder()},
            Decision(bucket="category", category_id="vax_gkb2",
                     folder="Вакцина ГКБ 2 Склиф (2 балла)", points=2))]
    prog = promotion_progress(old_period + now, R, swaps=sw)
    assert prog["have"]["vaccines"] == 1, "замена из сданного периода не считается"
