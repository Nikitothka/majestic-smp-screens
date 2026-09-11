# -*- coding: utf-8 -*-
"""Отменённые вызовы и пациент, названный по имени."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.extract import find_patient, match_contains, best_match, _phr
from medreport.config import load_rules
from medreport.score import decide

R = load_rules(personal=False)            # тесты не зависят от личных настроек машины
R.raw["promotions"]["target"] = "6-7"
R.raw["promotions"]["spare"] = {"vaccines": 1}
CONT = _phr["toasts_contains"]


def test_cancelled_call_recognised_with_any_reason():
    assert match_contains('Вызов от #79040 по причине "Смерть" был отменён!', CONT) == "call_cancelled"
    assert match_contains('Вызов от #1930 по причине "Смерть"; 5 был отменён!', CONT) == "call_cancelled"
    assert match_contains('Вызов от #500 по причине "Ранение" был отменён', CONT) == "call_cancelled"


def test_truncated_line_is_not_guessed():
    # OCR обрезал «отменён» — лучше в ручной разбор, чем записать вызов отменённым
    assert match_contains('Вызов от #82068 по причине "Смерть" был', CONT) is None
    assert match_contains("Вы реанимировали #76741", CONT) is None


def test_cancelled_call_scores_one_point_without_place():
    f = {"path": "x.png", "viewport_box": [0, 0, 1, 1], "action": "call_cancelled",
         "patient_id": None, "time": "2026-09-08T23:30", "time_source": "имя файла"}
    d = decide(f, None, R, indoor=(None, 0.0))
    assert d.category_id == "call_cancelled" and d.points == 1
    assert d.bucket == "category", "у отмены не спрашиваем ни место, ни номер пациента"


def test_patient_by_name():
    assert find_patient("Вы реанимировали Павел Ивлев") == "Павел Ивлев"
    assert find_patient("Вы вылечили Лев Морозов") == "Лев Морозов"
    assert find_patient("Вы реанимировали #76741") == "76741"
    assert find_patient("Вы получили 750 ₽ за спасение игрока") is None


def test_revive_outranks_cancelled_call_in_the_same_frame():
    """В кадре видно обе плашки — засчитываем реанимацию, а не отмену."""
    rows = ["Вы реанимировали #84178", 'Вызов от #79040 по причине "Смерть" был отменён!']
    found = []
    for row in rows:
        tag, score = best_match(row, {k: v for k, v in _phr["toasts"].items() if v})
        if score >= 0.72:
            found.append((tag, score))
        c = match_contains(row, CONT)
        if c:
            found.append((c, 1.0))
    order = _phr["priority"]
    rank = {a: i for i, a in enumerate(order)}
    winner = min(found, key=lambda t: (rank.get(t[0], 99), -t[1]))[0]
    assert winner == "revive"


# --------------------------------------------- имя игрока вместо номера
def test_named_patient_toasts_match_their_pattern():
    """«Вы вылечили Федор Щукин» — без замены имени совпадение падало до 0.6
    и кадр терялся, хотя действие в нём читается однозначно."""
    pats = {k: v for k, v in _phr["toasts"].items() if v}
    for text, want in [("Вы вылечили Федор Щукин", "heal"),
                       ("Вы реанимировали Павел Ивлев", "revive"),
                       ("Вы вакцинировали Лев Морозов", "vaccinate")]:
        tag, score = best_match(text, pats)
        assert tag == want and score >= 0.8, f"{text}: {tag} {score:.2f}"


def test_masking_names_does_not_create_false_actions():
    pats = {k: v for k, v in _phr["toasts"].items() if v}
    for text in ("Вы получили 750 ₽ за спасение игрока",
                 "Этот макрос уже проигрывается!",
                 "Иван Иванов начал оказывать первую медицинскую помощь для #4770"):
        tag, score = best_match(text, pats)
        assert score < 0.72, f"{text} не должен стать действием ({tag} {score:.2f})"
