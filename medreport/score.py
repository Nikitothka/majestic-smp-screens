# -*- coding: utf-8 -*-
"""Факты о скриншоте + место → категория, папка, баллы. Всё по rules.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime

from .config import Rules
from .place import PlaceGuess, HOSPITAL_LABELS

BUCKET_CATEGORY = "category"
BUCKET_MANUAL = "manual"
BUCKET_NO_ACTION = "no_action"

#: насколько кадр должен быть похож на знакомый больничный интерьер, чтобы верить
#: цвету стен. Салон скорой набирает ~0.47, настоящие больничные кадры — от 0.50,
#: и 95% из них выше 0.71.
HOSPITAL_LOOK_MIN = 0.60
#: для «ПМП в больнице» нужна уверенная похожесть: ниже — магазины и тоннели,
#: а между 0.60 и 0.66 решает человек
HOSPITAL_PMP_MIN = 0.66
#: эталоны решают, какая больница, только при таком перевесе одной над другой
KNN_GAP_MIN = 0.10

#: подпапки доп. баллов — по виду работы, без места и цены, чтобы не совпадать
#: с основными категориями
LUNCH_SUBFOLDER = {"revive": "ПМП в обед", "vaccinate": "Вакцины в обед",
                   "heal": "Таблетки в обед", "call_cancelled": "Отмены в обед",
                   "certificate": "Медсправки в обед"}


@dataclass
class Decision:
    bucket: str
    category_id: str | None = None
    folder: str = ""               # относительный путь папки назначения
    points: int = 0
    lunch: bool = False
    daypart: str | None = None
    place: str | None = None       # gkb1/gkb2/asmp/street
    place_source: str = ""
    in_hospital: bool | None = None
    reasons: list[str] = field(default_factory=list)   # почему в ручной разбор / пометки


def _parse_hhmm(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def in_window(t: dtime, window: list[str]) -> bool:
    a, b = _parse_hhmm(window[0]), _parse_hhmm(window[1])
    if a <= b:
        return a <= t < b
    return t >= a or t < b          # окно через полночь


def daypart_of(rules: Rules, t: dtime) -> str:
    return "day" if in_window(t, rules.times["day"]) else "night"


def resolve_place(action: str, facts: dict, guess: PlaceGuess | None, rules: Rules,
                  indoor: tuple[str | None, float] = (None, 0.0),
                  override: dict | None = None,
                  wall: tuple[str | None, float] = (None, 0.0)) -> tuple[str | None, bool | None, str, list[str]]:
    """Вернуть (место, в_больнице, источник, замечания) с точностью, нужной действию.

    Точность «hospital» решается моделью внутри/улица — она надёжна.
    Точность «exact» (какая именно больница) — только по ручной метке, прямой подписи
    на карте или эталонам интерьера: визуально больницы похожи, гадать нельзя.
    """
    need = rules.place_precision(action)
    notes: list[str] = []
    if need == "none":
        return None, None, "не требуется", notes

    in_label, in_margin = indoor

    # --- «в больнице или нет» ---------------------------------------------------
    if need == "hospital":
        if override:
            return override["place"], override["place"] in HOSPITAL_LABELS, override.get("source", "ручная метка"), notes
        # «ОПАСНО» — значит вне безопасной зоны, а больницы всегда безопасные.
        # Признак прямой и не зависит от того, узнала ли модель ракурс.
        if facts.get("zone_badge") == "danger":
            return "street", False, "значок ОПАСНО", notes
        if in_label == "street":
            return "street", False, "вид кадра (улица)", notes
        if in_label == "inside":
            # «под крышей» — ещё не больница: магазин и тоннель тоже помещения.
            # Решает похожесть на знакомые больничные интерьеры (проверено на кадрах,
            # отклонённых проверяющим: больница 0.71, магазин 0.56, тоннель 0.48).
            hs = facts.get("hospital_sim")
            if hs is None:
                return None, None, "", notes + ["в помещении, но больница ли — нечем сравнить"]
            if hs >= HOSPITAL_PMP_MIN:
                return "hospital", True, "похоже на больничный интерьер", notes
            if hs < HOSPITAL_LOOK_MIN:
                # для реанимации «street» значит «вне больницы» — так её и считают правила
                return "street", False, "помещение, но не больница (магазин, тоннель…)", notes
            return None, None, "", notes + [
                f"в помещении, но на больницу похоже слабо ({hs:.2f}) — проверь"]
        # ракурс в упор на плитку и толпу — «внутри или снаружи» на глаз не решается.
        # Тогда спрашиваем эталоны: если кадр похож на знакомый больничный интерьер, значит внутри.
        if guess and guess.label in HOSPITAL_LABELS and guess.top_sim >= 0.70:
            return "hospital", True, "похоже на знакомый больничный интерьер", notes
        return None, None, "", notes + ["не понял, в помещении кадр или на улице"]

    # --- «какая именно больница» -------------------------------------------------
    if override:
        return override["place"], True, override.get("source", "ручная метка"), notes

    map_place = facts.get("map_place")
    hits = facts.get("map_place_hits") or []
    if map_place in HOSPITAL_LABELS and any("БОЛЬНИЦА" in h for h in hits):
        return map_place, True, "подпись на карте", notes

    wall_label, wall_conf = wall
    if wall_label in HOSPITAL_LABELS:
        # цвет стен знает только про две больницы. Мед.отсек скорой он тоже назовёт больницей,
        # поэтому сперва убеждаемся, что кадр вообще похож на знакомый больничный интерьер.
        looks_like_hospital = guess is not None and guess.top_sim >= HOSPITAL_LOOK_MIN
        if not looks_like_hospital:
            return None, None, "", notes + [
                "по цвету стен похоже на больницу, но интерьер незнакомый — "
                "возможно, мед.отсек АСМП или другое помещение"]
        # спорить со сменой эталоны могут только при чётком перевесе одной больницы:
        # без него они путают похожие кабинеты (см. KNN_GAP_MIN)
        hs = facts.get("hosp_sims") or {}
        if "gkb1" in hs and "gkb2" in hs:
            knn_best = max(("gkb1", "gkb2"), key=lambda k: hs[k])
            strong = abs(hs["gkb1"] - hs["gkb2"]) >= KNN_GAP_MIN
        else:
            knn_best, strong = guess.label, guess.confidence > 0.75
        if knn_best in HOSPITAL_LABELS and knn_best != wall_label and strong:
            notes.append(f"цвет стен говорит {wall_label}, эталоны интерьера — {knn_best}")
            return None, None, "", notes + ["признаки места разошлись"]
        return wall_label, True, "цвет стен и освещение", notes

    # эталоны в одиночку: проверено на 112 кадрах с известной больницей — без условия
    # правы в 87% случаев, при разрыве похожести между больницами ≥ 0.10 — во всех.
    # Кабинеты и лаборатории в двух больницах похожи, поэтому без перевеса не угадываем.
    hs = facts.get("hosp_sims") or {}
    if "gkb1" in hs and "gkb2" in hs:
        best = max(("gkb1", "gkb2"), key=lambda k: hs[k])
        gap = abs(hs["gkb1"] - hs["gkb2"])
        if gap >= KNN_GAP_MIN:
            return best, True, "эталоны интерьера", notes
        return None, None, "", notes + [
            f"по интерьеру не отличить ГКБ 1 от Склифа (разрыв {gap:.2f}) — проверь"]
    if guess and guess.label in HOSPITAL_LABELS | {"asmp"}:
        return guess.label, guess.label in HOSPITAL_LABELS, "эталоны интерьера", notes

    if in_label == "street":
        notes.append("кадр похож на улицу, хотя такое действие возможно только в помещении")
    return None, None, "", notes + ["не размечено, какая это больница"]


def decide(facts: dict, guess: PlaceGuess | None, rules: Rules, *, knn_min_conf: float = 0.6,
           indoor: tuple[str | None, float] = (None, 0.0), override: dict | None = None,
           wall: tuple[str | None, float] = (None, 0.0)) -> Decision:
    d = Decision(bucket=BUCKET_MANUAL)
    if facts.get("viewport_box") is None:
        d.reasons.append("не найдено окно игры")
        return d
    action = facts.get("action")
    if not action:
        d.bucket = BUCKET_NO_ACTION
        return d
    if action != "call_cancelled" and not facts.get("patient_id"):
        d.reasons.append("не прочитан ID пациента")

    # время
    t_iso = facts.get("time")
    if not t_iso:
        d.reasons.append("нет времени")
        return d
    t = datetime.fromisoformat(t_iso)
    d.daypart = daypart_of(rules, t.time())
    lunch_ok = rules.lunch_applies_to == "all" or action in (rules.lunch_applies_to or [])
    d.lunch = lunch_ok and in_window(t.time(), rules.times["lunch"])
    if "ненадёжно" in (facts.get("time_source") or ""):
        d.reasons.append("время взято из файла, часы в игре не прочитаны")

    # место
    d.place, d.in_hospital, d.place_source, notes = resolve_place(
        action, facts, guess, rules, indoor=indoor, override=override, wall=wall)
    d.reasons.extend(notes)
    need = rules.place_precision(action)
    if need == "hospital" and d.in_hospital is None:
        return d
    if need == "exact" and d.place not in HOSPITAL_LABELS | {"asmp"}:
        if d.place == "street":
            d.reasons.append(f"{rules.actions[action]['title']} на улице — так не бывает, проверь")
        return d

    # категория
    attrs = {"action": action, "in_hospital": d.in_hospital, "place": d.place, "daypart": d.daypart}
    for cat in rules.categories:
        if all(attrs.get(k) == v for k, v in cat["match"].items()):
            d.category_id = cat["id"]
            d.points = int(cat["points"])
            d.folder = cat["folder"]
            break
    if d.category_id is None:
        d.reasons.append("ни одна категория не подошла")
        return d
    if d.lunch:
        # Отчёт вернули: подпапка «Вакцина АСМП (3 балла)» внутри «Доп. баллов» совпадала
        # по имени с основной категорией, и один скрин ушёл в форму дважды — и как АСМП,
        # и как доп. баллы. Подпапки по видам работы нужны (их выгружают отдельными
        # ссылками), но названы так, чтобы с основными категориями их не спутать.
        # Точная категория (для цены) хранится в журнале.
        d.folder = f"{rules.raw['lunch_folder']}/{LUNCH_SUBFOLDER.get(action, action)}"
        d.points *= 2
    # предупреждения не блокируют, если категория найдена и место определено
    d.bucket = BUCKET_CATEGORY if not any("не прочитан ID" in r for r in d.reasons) else BUCKET_MANUAL
    return d
