# -*- coding: utf-8 -*-
"""Разбор текста из HUD: часы, время в имени файла, подписи миникарты, фразы."""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.extract import (parse_clock, filename_time, match_map_place, best_match, merge_wrapped,
                               find_patient, ID_RE)
from medreport.config import load_phrases

P = load_phrases()


def test_clock_ignores_temperature_row():
    t, _ = parse_clock(['+18.49 Облачно', '15.24 07.09.2026', 'Понедельник', '656 168 ₽'])
    assert t == datetime(2026, 9, 7, 15, 24)


def test_clock_colon_and_dot_variants():
    assert parse_clock(['17:12 04.09.2026'])[0] == datetime(2026, 9, 4, 17, 12)
    assert parse_clock(["+23.2' Солнечно", '17.12 04.09.2026', 'Пятница'])[0] == datetime(2026, 9, 4, 17, 12)
    assert parse_clock(['Понедельник'])[0] is None


def test_filename_time_nvidia_and_steam():
    assert filename_time(Path("Grand Theft Auto V Screenshot 2026.09.07 - 15.24.16.67.png")) == datetime(2026, 9, 7, 15, 24, 16)
    assert filename_time(Path("20260902121357_1.jpg")) == datetime(2026, 9, 2, 12, 13, 57)
    assert filename_time(Path("Screenshot_45.png")) is None
    assert filename_time(Path("ыфвфывфыв.png")) is None


def test_map_label_with_ocr_typo_and_landmarks():
    assert match_map_place(['ЦУМ', 'ГУМ', '2 БОЛЬНИЦА', 'ФСБ'])[0] == "gkb2"
    assert match_map_place(['2 БОЛЕНИЦА', '8 АЗС'])[0] == "gkb2"
    assert match_map_place(['ПАВЕЛЕЦКАЯ', '82 ПВЗ ТЭ', '4 ОПГ 6 [', '1 ГШ'])[0] == "gkb1"
    assert match_map_place(['gUOg', 'ИПИ', 'БЕЗОПАСНО'])[0] is None


def test_map_conflict_returns_none():
    # по одному ориентиру на каждую больницу — не решаем
    lab, hits = match_map_place(['ЦУМ', 'ТЭЦ'])
    assert lab is None and len(hits) == 2


def test_toast_fuzzy_match_and_id():
    tag, score = best_match("Вы реанимировали #76741", P["toasts"])
    assert tag == "revive" and score > 0.95
    tag, _ = best_match("Bы вылечили #57405", P["toasts"])          # латинская B от OCR
    assert tag == "heal"
    tag, _ = best_match("Вы вакцинировапи #42650", P["toasts"])     # л→п
    assert tag == "vaccinate"
    assert ID_RE.search("Вы реанимировали #369").group(1) == "369"
    tag, score = best_match("Этот макрос уже проигрывается!", P["toasts"])
    assert score < 0.72, "служебная плашка не должна стать действием"


def test_toast_with_lost_hash_on_small_screen():
    """1366×768: решётка перед номером читается как «4», «5» или дефис (реальные строки OCR)."""
    for row, pid in (("Вы вылечили 474313", "74313"), ("Вы вылечили 589371", "89371"),
                     ("Вы вылечили -67225", "67225"), ("Вы вылечили 2431", "2431")):
        tag, score = best_match(row, P["toasts"])
        assert tag == "heal" and score >= 0.72, row
        assert find_patient(row) == pid, row
    assert find_patient("Вы реанимировали #89893") == "89893"
    assert find_patient("Вы вылечили Федор Щукин") == "Федор Щукин"
    # сумма награды — не действие, даже когда число превращается в «номер»
    assert best_match("Вы получили 750 ₽ за спасение игрока", P["toasts"])[1] < 0.72


def test_chat_hyphen_wrap_merge():
    rows = merge_wrapped(["Иван Иванов взял необходимый лекарственный препарат и пе-", "редал #57405"])
    assert rows == ["Иван Иванов взял необходимый лекарственный препарат и передал #57405"]


def test_clock_never_takes_time_from_the_date():
    # OCR теряет разделитель — раньше временем становился день с месяцем, и день превращался в ночь
    assert parse_clock(['1745 02.09.2026'])[0] == datetime(2026, 9, 2, 17, 45)
    assert parse_clock(['N1403 02.09.2026'])[0] == datetime(2026, 9, 2, 14, 3)
    assert parse_clock(['18:.50 03.09.2026'])[0] == datetime(2026, 9, 3, 18, 50)
    assert parse_clock(['19:56 02.09.2026', 'Среда'])[0] == datetime(2026, 9, 2, 19, 56)
    assert parse_clock(['WK BVHЭVdЯ 19.28 02.09.2026'])[0] == datetime(2026, 9, 2, 19, 28)
    assert parse_clock(['N4:34 04.09.2026'])[0] == datetime(2026, 9, 4, 4, 34)


def test_clock_gives_up_instead_of_guessing():
    # часы не читаются вовсе — лучше признаться, чем взять день с месяцем
    assert parse_clock(['Od 02.09.2026'])[0] is None
    assert parse_clock(['02.09.2026'])[0] is None
    assert parse_clock(['+18.4 Облачно', '02.09.2026'])[0] is None
