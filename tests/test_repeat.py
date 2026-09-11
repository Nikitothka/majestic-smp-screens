# -*- coding: utf-8 -*-
"""Повторный прогон не должен считать одни и те же кадры дважды."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport import sorter
from medreport.config import load_rules, SERVICE_DIRS
from medreport.score import Decision

R = load_rules(personal=False)            # тесты не зависят от личных настроек машины
R.raw["promotions"]["target"] = "6-7"
R.raw["promotions"]["spare"] = {"vaccines": 1}


def facts(path, h):
    return {"path": path, "hash": h, "viewport_box": [0, 0, 3840, 2160], "needs_crop": False,
            "action": "heal", "patient_id": "111", "time": "2026-09-08T16:25",
            "time_source": "имя файла"}


def cat():
    return Decision(bucket="category", category_id="pill_gkb1",
                    folder="Таблетка ГКБ 1 (1 балл)", points=1, place="gkb1")


def _journal(dest: Path) -> Path:
    return dest / SERVICE_DIRS["report"] / "journal.jsonl"


def test_second_run_sends_same_screenshot_to_duplicates(tmp_path):
    dest = tmp_path / "med"
    src = tmp_path / "a.png"
    src.write_bytes(b"one screenshot")
    journal = _journal(dest)

    plan = sorter.build_plan(dest, [(facts(str(src), "h1"), cat())], R)
    assert plan.moves[0].kind == "category"
    sorter.execute(plan, journal=journal, log=lambda *_: None)

    # тот же кадр приносят снова
    src.write_bytes(b"one screenshot")
    known = sorter.already_processed(journal)
    assert "h1" in known
    plan2 = sorter.build_plan(dest, [(facts(str(src), "h1"), cat())], R, known=known)
    assert plan2.moves[0].kind == "duplicate", "второй раз кадр не должен давать баллы"
    assert plan2.summary()[SERVICE_DIRS["duplicates"]]["points"] == 0
    assert plan2.moves[0].duplicate_of == known["h1"]


def test_registry_forgets_deleted_files(tmp_path):
    """Папку почистили — значит всё снова новое, реестр чистить руками не надо."""
    dest = tmp_path / "med"
    src = tmp_path / "a.png"
    src.write_bytes(b"one screenshot")
    journal = _journal(dest)
    plan = sorter.build_plan(dest, [(facts(str(src), "h1"), cat())], R)
    sorter.execute(plan, journal=journal, log=lambda *_: None)
    assert sorter.already_processed(journal) != {}

    Path(plan.moves[0].dst).unlink()
    assert sorter.already_processed(journal) == {}


def test_different_screenshots_are_not_duplicates(tmp_path):
    dest = tmp_path / "med"
    journal = _journal(dest)
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"first")
    b.write_bytes(b"second")
    plan = sorter.build_plan(dest, [(facts(str(a), "h1"), cat())], R)
    sorter.execute(plan, journal=journal, log=lambda *_: None)
    plan2 = sorter.build_plan(dest, [(facts(str(b), "h2"), cat())], R,
                              known=sorter.already_processed(journal))
    assert plan2.moves[0].kind == "category"


def test_undo_makes_screenshot_new_again(tmp_path):
    """После отката кадр можно разобрать заново — иначе откат был бы бесполезен."""
    dest = tmp_path / "med"
    src = tmp_path / "a.png"
    src.write_bytes(b"one screenshot")
    journal = _journal(dest)
    plan = sorter.build_plan(dest, [(facts(str(src), "h1"), cat())], R)
    sorter.execute(plan, journal=journal, log=lambda *_: None)
    sorter.undo_last(journal, log=lambda *_: None)
    assert sorter.already_processed(journal) == {}
    plan2 = sorter.build_plan(dest, [(facts(str(src), "h1"), cat())], R,
                              known=sorter.already_processed(journal))
    assert plan2.moves[0].kind == "category"
