# -*- coding: utf-8 -*-
"""Имена файлов, коллизии, дубликаты, записка для ручного разбора."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport import sorter
from medreport.config import load_rules, SERVICE_DIRS
from medreport.score import Decision

R = load_rules(personal=False)            # тесты не зависят от личных настроек машины
R.raw["promotions"]["target"] = "6-7"
R.raw["promotions"]["spare"] = {"vaccines": 1}


def _facts(path, h, action="heal", pid="82710", t="2026-09-04T16:25", crop=False):
    return {"path": path, "hash": h, "viewport_box": [0, 0, 3840, 2160], "needs_crop": crop,
            "action": action, "patient_id": pid, "time": t, "time_source": "часы в игре", "safe_zone": True}


def test_target_name_has_time_action_id_and_place_for_pills():
    d = Decision(bucket="category", category_id="pill_gkb1", folder="Таблетка ГКБ 1 (1 балл)", points=1, place="gkb1")
    assert sorter.target_name(_facts("a.png", "h1"), d, ".png") == "2026-09-04_16-25_таблетка_82710_ГКБ1.png"
    d = Decision(bucket="category", category_id="pmp_day", folder="ПМП день (3 балла)", points=3, place="street")
    assert sorter.target_name(_facts("a.png", "h1", action="revive", pid="4770"), d, ".jpg") == "2026-09-04_16-25_реанимация_4770.jpg"


def test_plan_routes_buckets_and_duplicates(tmp_path):
    dest = tmp_path / "med"
    cat = Decision(bucket="category", category_id="pill_gkb1", folder="Таблетка ГКБ 1 (1 балл)", points=1, place="gkb1")
    man = Decision(bucket="manual", reasons=["место не определено"])
    noa = Decision(bucket="no_action")
    items = [(_facts("x/a.png", "h1"), cat), (_facts("x/b.png", "h1"), cat),   # b — дубликат a
             (_facts("x/c.png", "h2"), man), (_facts("x/d.png", "h3", crop=True), noa)]
    plan = sorter.build_plan(dest, items, R)
    kinds = [m.kind for m in plan.moves]
    assert kinds == ["category", "duplicate", "manual", "no_action"]
    assert Path(plan.moves[0].dst).parent.name == "Таблетка ГКБ 1 (1 балл)"
    assert Path(plan.moves[1].dst).parent.name == SERVICE_DIRS["duplicates"]
    assert plan.moves[2].sidecar.endswith(".txt") and "место не определено" in plan.moves[2]._sidecar_text
    assert plan.moves[3].crop and Path(plan.moves[3].original_dst).parent.name == SERVICE_DIRS["originals"]
    assert plan.moves[3].dst.endswith(".png"), "обрезанный всегда png"
    s = plan.summary()
    assert s["Таблетка ГКБ 1 (1 балл)"] == {"files": 1, "points": 1}


def test_plan_avoids_name_collisions(tmp_path):
    cat = Decision(bucket="category", category_id="pill_gkb1", folder="Таблетка ГКБ 1 (1 балл)", points=1, place="gkb1")
    items = [(_facts("x/a.png", "h1"), cat), (_facts("x/b.png", "h2"), cat)]   # разное содержимое, одинаковое имя
    plan = sorter.build_plan(tmp_path, items, R)
    names = {Path(m.dst).name for m in plan.moves}
    assert len(names) == 2
