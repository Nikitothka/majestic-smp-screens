# -*- coding: utf-8 -*-
"""Баллы: день/ночь/обед и точность места по типу действия.

Ночных скринов в наборе нет (максимум 19:56), поэтому ночная ветка проверяется
здесь подменой времени.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.config import load_rules
from medreport.score import decide
from medreport.place import PlaceGuess

R = load_rules(personal=False)            # тесты не зависят от личных настроек машины
R.raw["promotions"]["target"] = "6-7"
R.raw["promotions"]["spare"] = {"vaccines": 1}
STREET = ("street", 0.20)
INSIDE = ("inside", 0.20)
UNSURE = (None, 0.001)


def facts(action="revive", t="2026-09-04T15:03", pid="71844", map_place=None, hits=None):
    return {"path": "x.png", "viewport_box": [0, 0, 1, 1], "action": action, "patient_id": pid,
            "time": t, "time_source": "имя файла", "map_place": map_place, "map_place_hits": hits or []}


def g(label, conf=0.9):
    return PlaceGuess(label, conf, 0.9, {label: 1.0})


# ------------------------------------------------------------------ реанимация
def test_revive_street_day_and_night():
    d = decide(facts(t="2026-09-04T15:03"), None, R, indoor=STREET)
    assert d.category_id == "pmp_day" and d.points == 3 and not d.lunch
    assert decide(facts(t="2026-09-04T23:10"), None, R, indoor=STREET).category_id == "pmp_night"
    assert decide(facts(t="2026-09-05T03:00"), None, R, indoor=STREET).points == 5
    assert decide(facts(t="2026-09-05T09:59"), None, R, indoor=STREET).category_id == "pmp_night"
    assert decide(facts(t="2026-09-05T10:00"), None, R, indoor=STREET).category_id == "pmp_day"


def _in_hospital(t):
    f = facts(t=t)
    f["hospital_sim"] = 0.75                        # уверенно похоже на больничный холл
    return f


def test_revive_in_hospital_is_one_point_regardless_of_hour():
    d = decide(_in_hospital("2026-09-04T15:03"), None, R, indoor=INSIDE)
    assert d.category_id == "pmp_hospital" and d.points == 1
    d = decide(_in_hospital("2026-09-04T23:03"), None, R, indoor=INSIDE)
    assert d.category_id == "pmp_hospital" and d.points == 1, "ночь не важна в больнице"


def test_revive_does_not_need_to_know_which_hospital():
    d = decide(_in_hospital("2026-09-04T17:23"), None, R, indoor=INSIDE)
    assert d.bucket == "category", "для ПМП неважно, ГКБ 1 это или Склиф"


def test_revive_unsure_indoor_goes_manual():
    d = decide(facts(), None, R, indoor=UNSURE)
    assert d.bucket == "manual" and any("в помещении" in r for r in d.reasons)


# ------------------------------------------------- таблетки, вакцины: точное место
def test_exact_place_from_direct_map_label():
    f = facts(action="heal", t="2026-09-04T16:25", map_place="gkb1", hits=["1 БОЛЬНИЦА"])
    d = decide(f, g("gkb2"), R, indoor=INSIDE)
    assert d.category_id == "pill_gkb1" and d.place_source == "подпись на карте"


def test_landmarks_without_direct_label_are_not_enough():
    # ориентиры вокруг говорят, где кадр, но не в какой больнице смена
    f = facts(action="heal", t="2026-09-04T16:25", map_place="gkb1", hits=["ПАВЕЛЕЦКАЯ", "КАЗИНО"])
    d = decide(f, None, R, indoor=INSIDE)
    assert d.bucket == "manual" and any("не размечено" in r for r in d.reasons)


def test_manual_override_wins():
    ov = {"place": "gkb2", "source": "разметка смены"}
    d = decide(facts(action="vaccinate", t="2026-09-04T17:23"), None, R, indoor=INSIDE, override=ov)
    assert d.category_id == "vax_gkb2" and d.points == 2 and d.place_source == "разметка смены"


def test_interior_refs_used_when_nothing_better():
    d = decide(facts(action="heal", t="2026-09-04T16:25"), g("gkb2"), R, indoor=INSIDE)
    assert d.category_id == "pill_gkb2" and d.place_source == "эталоны интерьера"


def test_exact_place_unknown_goes_manual():
    d = decide(facts(action="vaccinate", t="2026-09-04T17:23"), None, R, indoor=INSIDE)
    assert d.bucket == "manual"


def test_pill_looking_like_street_is_flagged():
    d = decide(facts(action="heal", t="2026-09-04T16:25"), None, R, indoor=STREET)
    assert d.bucket == "manual" and any("похож на улицу" in r for r in d.reasons)


# ------------------------------------------------------------------------- обед
def test_lunch_doubles_and_moves_to_extra_folder():
    d = decide(facts(t="2026-09-04T14:56"), None, R, indoor=STREET)
    assert d.lunch and d.points == 6 and d.folder.startswith(R.raw["lunch_folder"])
    ov = {"place": "gkb1", "source": "разметка смены"}
    d = decide(facts(action="heal", t="2026-09-04T14:34"), None, R, indoor=INSIDE, override=ov)
    assert d.lunch and d.category_id == "pill_gkb1" and d.points == 2
    assert not decide(facts(t="2026-09-04T15:00"), None, R, indoor=STREET).lunch, "15:00 уже не обед"
    assert not decide(facts(t="2026-09-04T13:59"), None, R, indoor=STREET).lunch


# ------------------------------------------------------------------- служебное
def test_no_action_and_no_viewport():
    assert decide({"path": "x", "viewport_box": None}, None, R).bucket == "manual"
    assert decide({"path": "x", "viewport_box": [0, 0, 1, 1], "action": None}, None, R).bucket == "no_action"


def test_missing_patient_id_goes_manual():
    f = facts(pid=None)
    d = decide(f, None, R, indoor=STREET)
    assert d.bucket == "manual" and any("ID" in r for r in d.reasons)


# ------------------------------------------------------- место по цвету стен
def test_wall_colour_decides_hospital():
    # цвету стен верим только в знакомом больничном интерьере, см. test_wall_colour_not_trusted_...
    near1 = PlaceGuess("gkb1", 0.9, 0.82, {"gkb1": 1.0})
    near2 = PlaceGuess("gkb2", 0.9, 0.82, {"gkb2": 1.0})
    d = decide(facts(action="heal", t="2026-09-04T16:25"), near1, R, indoor=INSIDE, wall=("gkb1", 0.04))
    assert d.category_id == "pill_gkb1" and d.place_source == "цвет стен и освещение"
    d = decide(facts(action="vaccinate", t="2026-09-04T17:23"), near2, R, indoor=INSIDE, wall=("gkb2", 0.04))
    assert d.category_id == "vax_gkb2" and d.points == 2


def test_manual_override_beats_wall_colour():
    ov = {"place": "gkb1", "source": "разметка смены"}
    d = decide(facts(action="heal", t="2026-09-04T16:25"), PlaceGuess("gkb2", 0.9, 0.82, {}), R,
               indoor=INSIDE, override=ov, wall=("gkb2", 0.04))
    assert d.category_id == "pill_gkb1" and d.place_source == "разметка смены"


def test_wall_and_interior_conflict_goes_manual():
    d = decide(facts(action="heal", t="2026-09-04T16:25"), g("gkb1", conf=0.9), R,
               indoor=INSIDE, wall=("gkb2", 0.04))
    assert d.bucket == "manual" and any("разошлись" in r for r in d.reasons)


def test_wall_colour_not_trusted_in_unfamiliar_room():
    """Салон скорой по цвету похож на Склиф — но интерьер незнакомый, значит спрашиваем."""
    far = PlaceGuess("gkb2", 0.5, 0.47, {"gkb2": 1.0})       # ближайший эталон далеко
    d = decide(facts(action="heal", t="2026-09-08T17:59"), far, R, indoor=INSIDE, wall=("gkb2", 0.04))
    assert d.bucket == "manual" and any("АСМП" in r for r in d.reasons)


def test_wall_colour_trusted_in_familiar_hospital():
    near = PlaceGuess("gkb2", 0.9, 0.82, {"gkb2": 1.0})
    d = decide(facts(action="heal", t="2026-09-08T17:59"), near, R, indoor=INSIDE, wall=("gkb2", 0.04))
    assert d.category_id == "pill_gkb2" and d.place_source == "цвет стен и освещение"


def test_asmp_override_still_wins():
    ov = {"place": "asmp", "source": "разметка"}
    far = PlaceGuess("gkb2", 0.5, 0.47, {"gkb2": 1.0})
    d = decide(facts(action="heal", t="2026-09-08T17:59"), far, R, indoor=INSIDE,
               override=ov, wall=("gkb2", 0.04))
    assert d.category_id == "pill_asmp" and d.points == 2


# ------------------------------------------------------- нормативы повышения
def _pack(pmp=0, vax=0, pmp_points=3):
    """Набор кадров для проверки нормативов."""
    D = decide.__globals__["Decision"]
    return ([({}, D(bucket="category", category_id="pmp_day",
                    folder="ПМП день (3 балла)", points=pmp_points)) for _ in range(pmp)]
            + [({}, D(bucket="category", category_id="vax_gkb2",
                      folder="Вакцина ГКБ 2 Склиф (2 балла)", points=2)) for _ in range(vax)])


def test_promotion_progress_and_substitutions():
    """Проверяем арифметику, а не конкретный ранг: цель в rules.yaml меняется."""
    from medreport.report import promotion_progress, substitution_plan

    need = R.raw["promotions"]["ranks"][R.raw["promotions"]["target"]]
    vax = max(0, int(need["vaccines"]) - 2)
    # ПМП с запасом и дорогие (обеденные) — значит и баллов вдоволь
    items = _pack(pmp=int(need["pmp"]) + 40, vax=vax, pmp_points=6)
    prog = promotion_progress(items, R, swaps=[])
    assert prog["gaps"]["pmp"] == 0 and prog["gaps"]["points"] == 0
    assert prog["gaps"]["vaccines"] == 2

    plan = substitution_plan(prog, items, R)
    assert plan, "запаса ПМП и баллов хватает, чтобы докупить вакцины"
    x = plan[0]
    spare = (R.raw["promotions"].get("spare") or {}).get("vaccines", 0)
    assert x["give_what"] == "revive" and x["get_what"] == "vaccinate"
    assert x["get"] == 2 + spare, "закрываем дыру и берём настроенный запас сверх нормы"
    assert x["delta_points"] < 0, "замена всегда стоит баллов, а не приносит их"


def test_substitution_refused_when_points_would_drop_below_norm():
    """Баллов впритык — менять нельзя: обмен уронит их ниже норматива."""
    from medreport.report import promotion_progress, substitution_plan

    need = R.raw["promotions"]["ranks"][R.raw["promotions"]["target"]]
    vax = max(0, int(need["vaccines"]) - 2)
    pmp = (int(need["points"]) - vax * 2) // 3 + 1        # баллы чуть выше нормы
    items = _pack(pmp=pmp, vax=vax, pmp_points=3)
    prog = promotion_progress(items, R, swaps=[])
    assert prog["gaps"]["points"] == 0 and prog["gaps"]["vaccines"] == 2
    assert prog["have"]["points"] - int(need["points"]) < 10, "запас по баллам должен быть мал"
    plan = substitution_plan(prog, items, R)
    assert not plan, "обмен уронил бы баллы ниже нормы — предлагать его нельзя"


def test_substitution_never_eats_into_the_quota():
    """ПМП ровно по норме — менять нечего, иначе просядет сам норматив по ПМП."""
    from medreport.report import promotion_progress, substitution_plan

    need = R.raw["promotions"]["ranks"][R.raw["promotions"]["target"]]
    items = _pack(pmp=int(need["pmp"]), pmp_points=100)   # баллов вдоволь, ПМП впритык
    prog = promotion_progress(items, R, swaps=[])
    plan = substitution_plan(prog, items, R)
    assert not any(x["give_what"] == "revive" for x in plan)


def test_hospital_pmp_does_not_count_towards_promotion():
    from medreport.report import promotion_progress
    D = decide.__globals__["Decision"]
    items = [({}, D(bucket="category", category_id="pmp_hospital",
                    folder="ПМП в больнице (1 балл)", points=1)) for _ in range(10)]
    prog = promotion_progress(items, R, swaps=[])
    assert prog["have"]["pmp"] == 0, "ПМП в больнице в норматив не идёт"
    assert prog["have"]["points"] == 10, "но баллы даёт"


def test_plural_forms():
    from medreport.report import plural
    assert plural(1, "vaccinate") == "1 вакцина"
    assert plural(3, "vaccinate") == "3 вакцины"
    assert plural(7, "vaccinate") == "7 вакцин"
    assert plural(21, "points") == "21 балл"


def test_danger_badge_means_not_in_hospital():
    """«ОПАСНО» — вне безопасной зоны, а больница всегда безопасная.
    Работает даже когда ракурс модели незнаком (кадр в кустах)."""
    f = facts(t="2026-09-10T21:08")
    f["zone_badge"] = "danger"
    d = decide(f, None, R, indoor=UNSURE)          # модель не поняла кадр
    assert d.category_id == "pmp_day" and d.place == "street"
    assert d.place_source == "значок ОПАСНО"


def test_safe_badge_alone_does_not_prove_hospital():
    f = facts(t="2026-09-10T21:08")
    f["zone_badge"] = "safe"
    d = decide(f, None, R, indoor=UNSURE)
    assert d.bucket == "manual", "безопасная зона — не обязательно больница"


# ------------------------- «ПМП в больнице»: помещение — ещё не больница
# Отчёт отклонили: из трёх «ПМП в больнице» в больнице был один. Остальные —
# магазин 24/7 и автомобильный тоннель: под крышей, но не больница.
def _revive_inside(sim, zone=None):
    f = facts(t="2026-09-10T18:52")
    f["hospital_sim"] = sim
    f["zone_badge"] = zone
    return decide(f, None, R, indoor=INSIDE)


def test_real_hospital_is_hospital_pmp():
    d = _revive_inside(0.714, zone="safe")          # холл ГКБ 1
    assert d.category_id == "pmp_hospital" and d.points == 1


def test_shop_is_not_hospital_even_in_safe_zone():
    d = _revive_inside(0.564, zone="safe")          # магазин 24/7
    assert d.category_id == "pmp_day" and d.points == 3


def test_tunnel_is_not_hospital():
    d = _revive_inside(0.481)                       # автомобильный тоннель
    assert d.category_id == "pmp_day" and d.points == 3


def test_borderline_similarity_goes_to_human():
    d = _revive_inside(0.63, zone="safe")
    assert d.bucket == "manual" and any("похоже слабо" in r for r in d.reasons)


def test_inside_without_any_reference_is_not_guessed():
    f = facts(t="2026-09-10T18:52")
    d = decide(f, None, R, indoor=INSIDE)           # hospital_sim не посчитан
    assert d.bucket == "manual"


# ------------------------------- доп. баллы: один скрин — одно место
def test_lunch_subfolder_never_repeats_a_main_category_name():
    """Отчёт вернули: подпапка «Вакцина АСМП (3 балла)» внутри «Доп. баллов»
    совпадала с основной категорией, и скрин попал в форму дважды."""
    from medreport.score import LUNCH_SUBFOLDER
    main = {c["folder"] for c in R.categories}
    for sub in LUNCH_SUBFOLDER.values():
        assert sub not in main, f"«{sub}» совпадает с основной категорией"
    ov = {"place": "asmp", "source": "тест"}
    d = decide(facts(action="vaccinate", t="2026-09-10T14:08"), None, R, indoor=INSIDE, override=ov)
    assert d.lunch and d.category_id == "vax_asmp" and d.points == 6
    assert d.folder == f"{R.raw['lunch_folder']}/Вакцины в обед"
    assert "АСМП" not in d.folder


def test_report_lists_lunch_screenshot_only_once():
    from medreport.report import category_block
    rows = [{"статус": "разобрано", "категория": "vax_asmp", "обед": "да",
             "действие": "", "баллы": 6},          # подпись пустая — как у устаревшей карточки
            {"статус": "разобрано", "категория": "vax_gkb1", "обед": "",
             "действие": "вакцинация", "баллы": 3}]
    lines, total = category_block(rows, R)
    text = "\n".join(lines)
    assert "Вакцина АСМП" not in text, "обеденная вакцина не должна стоять в основной категории"
    assert "Вакцины: 1 шт = 6 б." in text
    assert total == 9


# ------------------ какая больница: эталоны решают только при уверенном перевесе
# Проверено на 112 кадрах с известной больницей: в одиночку эталоны правы в 87%,
# при разрыве похожести между больницами ≥ 0.10 — во всех. Кабинеты похожи.
def _vax(hs):
    f = facts(action="vaccinate", t="2026-09-10T13:07")
    f["hosp_sims"] = hs
    return decide(f, None, R, indoor=INSIDE)


def test_interior_decides_hospital_only_with_clear_margin():
    d = _vax({"gkb1": 0.82, "gkb2": 0.70})
    assert d.category_id == "vax_gkb1" and d.place_source == "эталоны интерьера"


def test_close_interiors_go_to_human_not_guessed():
    # тот самый кабинет ГКБ 1 (10.09 13:07), который эталоны отнесли в Склиф
    d = _vax({"gkb1": 0.716, "gkb2": 0.760})
    assert d.bucket == "manual" and any("не отличить" in r for r in d.reasons)


def test_session_colour_still_wins_over_close_interiors():
    f = facts(action="vaccinate", t="2026-09-10T13:07")
    f["hosp_sims"] = {"gkb1": 0.716, "gkb2": 0.760}
    near = PlaceGuess("gkb2", 0.9, 0.76, {"gkb2": 1.0})
    d = decide(f, near, R, indoor=INSIDE, wall=("gkb1", 0.04))
    assert d.category_id == "vax_gkb1", "цвет регистратуры по смене важнее похожести кабинета"
