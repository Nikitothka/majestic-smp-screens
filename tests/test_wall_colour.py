# -*- coding: utf-8 -*-
"""Модель «какая больница по цвету стен»: находит две группы и привязывает их к названиям."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medreport.place import WallColorModel

COLD = [-0.044, -0.038, -0.049, -0.035, -0.052, -0.041, -0.036]   # серо-голубая больница
WARM = [+0.039, +0.042, +0.033, +0.045, +0.036, +0.041, +0.038]   # бежевая


def values(cold=COLD, warm=WARM):
    d = {f"c{i}": v for i, v in enumerate(cold)}
    d.update({f"w{i}": v for i, v in enumerate(warm)})
    return d


def test_two_groups_anchored_by_one_confirmed_screenshot():
    m = WallColorModel.fit(values(), {"c0": "gkb2"}, log=lambda *_: None)
    assert m.ready
    assert m.predict(-0.045)[0] == "gkb2"
    assert m.predict(+0.040)[0] == "gkb1", "вторая группа — та, другая из двух больниц"


def test_grey_zone_returns_nothing():
    m = WallColorModel.fit(values(), {"c0": "gkb2"}, log=lambda *_: None)
    assert m.predict(0.0)[0] is None
    assert m.predict(None)[0] is None


def test_without_anchor_model_stays_silent():
    m = WallColorModel.fit(values(), {}, log=lambda *_: None)
    assert not m.ready and m.predict(-0.045)[0] is None


def test_single_hospital_is_not_split():
    m = WallColorModel.fit(values(warm=[-0.040, -0.042, -0.039, -0.041, -0.043, -0.038, -0.037]),
                           {"c0": "gkb2"}, log=lambda *_: None)
    assert not m.ready, "одна больница не должна делиться надвое"


def test_roundtrip(tmp_path):
    m = WallColorModel.fit(values(), {"w0": "gkb1"}, log=lambda *_: None)
    f = tmp_path / "wall.json"
    m.save(f)
    m2 = WallColorModel.load(f)
    assert m2.ready and m2.predict(-0.045)[0] == m.predict(-0.045)[0]
