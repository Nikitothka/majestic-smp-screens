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
