# -*- coding: utf-8 -*-
"""Поиск окна игры: разные разрешения и раскладки кадра."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.viewport import Box, _whole_frame, _viewport_from_logo


def test_true_4k_frame_is_accepted():
    """3840×2160: логотип меряется 126 px вместо 123, и расчёт ширины по нему
    уводил левый край на −92 px. Кадр отвергался целиком."""
    logo = Box(3683, 66, 3683 + 126, 66 + 41)
    vp = _whole_frame(logo, 3840, 2160)
    assert vp is not None
    assert (vp.box.x0, vp.box.y0, vp.box.x1, vp.box.y1) == (0, 0, 3840, 2160)
    assert vp.is_full_frame


def test_nvidia_and_steam_frames():
    for W, H, x0 in ((3832, 2114, 3680), (3834, 2114, 3680)):
        logo = Box(x0, 64, x0 + 123, 64 + 40)
        vp = _whole_frame(logo, W, H)
        assert vp is not None and vp.box.x1 == W


def test_dual_monitor_frame_is_not_whole_frame():
    """5760×2160 — соотношение сторон 2.67, это не окно игры, а рабочий стол."""
    logo = Box(3686, 104, 3686 + 123, 104 + 41)
    assert _whole_frame(logo, 5760, 2160) is None
    vp = _viewport_from_logo(logo, 5760, 2160)
    assert vp is not None and vp.box.x0 == 0 and 3700 < vp.box.x1 <= 3900


def test_logo_far_from_corner_is_rejected():
    """Красный блок посреди кадра — не логотип HUD."""
    logo = Box(1000, 800, 1123, 840)
    assert _whole_frame(logo, 3840, 2160) is None
    assert _viewport_from_logo(logo, 3840, 2160) is None


def _corner_logo(W, H):
    """Логотип там, где его рисует игра: интерфейс растёт с высотой окна."""
    s = H / 2160
    w, h = round(126 * s), round(41 * s)
    x1, y0 = W - round(31 * s), round(66 * s)
    return Box(x1 - w, y0, x1, y0 + h)


def test_small_screens_are_found():
    """На 1080p логотип вдвое меньше, чем в 4K, — раньше такие кадры не находились вовсе."""
    for W, H in ((2560, 1440), (1920, 1080), (1600, 900), (1366, 768), (1920, 1200)):
        vp = _whole_frame(_corner_logo(W, H), W, H)
        assert vp is not None, (W, H)
        assert (vp.box.x1, vp.box.y1) == (W, H) and abs(vp.scale - H / 2160) < 1e-6


def test_zones_scale_with_the_screen():
    """Зона плашек на 1080p — ровно половина той же зоны в 4K."""
    big = _whole_frame(_corner_logo(3840, 2160), 3840, 2160).zone_toasts
    small = _whole_frame(_corner_logo(1920, 1080), 1920, 1080).zone_toasts
    for a, b in zip((big.x0, big.y0, big.x1, big.y1), (small.x0, small.y0, small.x1, small.y1)):
        assert abs(a / 2 - b) <= 1


def test_ultrawide_keeps_right_side_zones_at_the_right_edge():
    """21:9: интерфейс того же размера, что на 16:9 той же высоты, но окно шире.
    ID игрока стоит у правого края, и зона должна остаться там, а не уехать в середину."""
    vp = _whole_frame(_corner_logo(3440, 1440), 3440, 1440)
    assert vp is not None
    assert vp.zone_topright.x1 == 3440 and vp.zone_topright.x0 > 3000
    t = vp.zone_toasts
    assert abs((t.x0 + t.x1) / 2 - 1720) <= 1        # плашки — по центру


def test_small_red_block_in_the_corner_is_not_a_logo():
    """Мелкий красный предмет в углу 4K-кадра — не логотип: размер не тот."""
    logo = Box(3760, 70, 3800, 83)
    assert _whole_frame(logo, 3840, 2160) is None


def test_dual_monitor_with_1080p_game_window():
    """Два монитора 1080p в одном кадре: игра слева, справа рабочий стол."""
    logo = _corner_logo(1920, 1080)
    vp = _viewport_from_logo(logo, 3840, 1080)
    assert _whole_frame(logo, 3840, 1080) is None
    assert vp is not None and vp.box.x0 == 0 and 1880 <= vp.box.x1 <= 1960
