# -*- coding: utf-8 -*-
"""Поиск окна игры в кадре и разметка зон HUD.

Якорь — красный логотип «РОССИЯ ОНЛАЙН» в правом верхнем углу вьюпорта.
От него считаются правый край, верх и горизонтальный центр вьюпорта; низ — низ кадра
(окно игры во всех наблюдавшихся раскладках доходит до низа).

Интерфейс игры растёт вместе с высотой окна: при высоте 2114 px логотип 123 px шириной,
при 2160 — 126, значит на 1080p он около 63, на 768p — около 45. Поэтому логотип ищется
любого размера, а все зоны HUD задаются в единицах окна высотой 2160 px и умножаются
на масштаб. Зоны привязаны к тому краю, у которого стоит элемент: часы — к левому,
ID — к правому, плашки — к центру. Так они не разъезжаются и на широких мониторах 21:9.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

# Эталонные размеры логотипа при масштабе UI 1.0 (вьюпорт 3840 px шириной)
LOGO_W, LOGO_H = 123, 40
# Смещения вьюпорта относительно логотипа (измерено на четырёх раскладках)
CENTER_FROM_LOGO_RIGHT = 1890   # центр вьюпорта = правый край логотипа − 1890
TOP_FROM_LOGO_TOP = 64          # верх вьюпорта = верх логотипа − 64
HALF_WIDTH = 1920
REF_H = 2160                    # высота окна, в которой заданы все зоны HUD
# Самый мелкий логотип, который ещё отличим от шума: 1366×768 даёт ~45×15 px
LOGO_MIN_W, LOGO_MIN_H = 30, 10
# Окно игры целиком в кадре: от 4:3 до 21:9. Шире — это уже два монитора.
WHOLE_AR = (1.3, 2.45)


@dataclass(frozen=True)
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    def clamp(self, W: int, H: int) -> "Box":
        return Box(max(0, self.x0), max(0, self.y0), min(W, self.x1), min(H, self.y1))

    def crop(self, im: Image.Image) -> Image.Image:
        return im.crop((self.x0, self.y0, self.x1, self.y1))


@dataclass
class Viewport:
    box: Box            # окно игры внутри кадра
    scale: float        # масштаб UI относительно эталона
    logo: Box
    frame_w: int
    frame_h: int

    @property
    def is_full_frame(self) -> bool:
        """Игра занимает весь кадр — обрезать не нужно."""
        return (self.box.x0 <= 4 and self.box.y0 <= 4
                and self.frame_w - self.box.x1 <= 12 and self.frame_h - self.box.y1 <= 4)

    @property
    def cx(self) -> int:
        return (self.box.x0 + self.box.x1) // 2

    @classmethod
    def from_box(cls, box, frame_w: int, frame_h: int) -> "Viewport":
        """Окно по сохранённым координатам — масштаб восстанавливается по его высоте."""
        b = Box(*box)
        return cls(box=b, scale=b.h / REF_H, logo=Box(0, 0, 1, 1), frame_w=frame_w, frame_h=frame_h)

    # --- зоны HUD (в координатах кадра) ---------------------------------
    def _z(self, x0, y0, x1, y1) -> Box:
        b, s = self.box, self.scale
        return Box(int(b.x0 + x0 * s), int(b.y0 + y0 * s),
                   int(b.x0 + x1 * s), int(b.y0 + y1 * s)).clamp(self.frame_w, self.frame_h)

    def _zb(self, x0, dy0, x1, dy1) -> Box:
        """Зона, привязанная к низу вьюпорта: dy — отступ от низа."""
        b, s = self.box, self.scale
        return Box(int(b.x0 + x0 * s), int(b.y1 - dy0 * s),
                   int(b.x0 + x1 * s), int(b.y1 - dy1 * s)).clamp(self.frame_w, self.frame_h)

    def _zr(self, dx0, y0, dx1, y1) -> Box:
        """Зона, привязанная к правому краю вьюпорта: dx — отступ от правого края."""
        b, s = self.box, self.scale
        return Box(int(b.x1 - dx0 * s), int(b.y0 + y0 * s),
                   int(b.x1 - dx1 * s), int(b.y0 + y1 * s)).clamp(self.frame_w, self.frame_h)

    @property
    def zone_toasts(self) -> Box:
        c = self.cx
        s = self.scale
        return Box(int(c - 470 * s), int(self.box.y1 - 470 * s),
                   int(c + 470 * s), int(self.box.y1 - 20 * s)).clamp(self.frame_w, self.frame_h)

    @property
    def zone_clock(self) -> Box:      # часы, дата, день недели, погода, деньги
        return self._zb(560, 310, 1290, 20)

    @property
    def zone_safezone(self) -> Box:   # значок БЕЗОПАСНО
        return self._zb(60, 160, 420, 40)

    @property
    def zone_minimap(self) -> Box:
        return self._zb(0, 600, 620, 0)

    @property
    def zone_chat(self) -> Box:
        return self._z(0, 60, 1000, 1000)

    @property
    def zone_call_panel(self) -> Box:
        return self._z(0, 700, 560, 1300)

    @property
    def zone_topright(self) -> Box:   # онлайн, ID игрока
        return self._zr(540, 70, 0, 260)

    @property
    def zone_scene(self) -> Box:
        """Игровая сцена без HUD — для эмбеддинга интерьера.

        На 16:9 это x 700…3100 из 3840. На широком окне берётся та же по размеру середина,
        чтобы эталоны, снятые на 16:9, сравнивались с тем же куском сцены.
        """
        b, s = self.box, self.scale
        half, c = 1200 * s, self.cx - 20 * s
        return Box(int(c - half), int(b.y0 + 250 * s),
                   int(c + half), int(b.y0 + 1650 * s)).clamp(self.frame_w, self.frame_h)


def _plate_fill(reg: np.ndarray) -> float:
    """Доля рамки, занятая красной плашкой вместе с белыми буквами на ней.

    Чисто красного в логотипе всего половина: остальное — надпись. На 1080p и в JPEG
    края букв размываются в розовое, и доля чистого красного падает до 0.44, хотя
    это тот же логотип. Красный, белый и розовый вместе дают 0.99 на любом экране,
    а у случайного красного предмета в рамку попадает фон сцены.
    """
    r, g, b = (reg[..., i].astype(np.int16) for i in range(3))
    red = (r > 110) & (r - g > 45) & (r - b > 45)
    white = np.minimum(np.minimum(r, g), b) > 150
    pink = (r > 150) & (r - np.maximum(g, b) > 20)
    return float((red | white | pink).mean())


def _logo_candidates(arr: np.ndarray) -> list[Box]:
    """Все насыщенно-красные сплошные плашки, похожие на логотип, — от самой похожей к менее."""
    r, g, b = arr[..., 0].astype(np.int16), arr[..., 1].astype(np.int16), arr[..., 2].astype(np.int16)
    mask = (r > 150) & (g < 90) & (b < 90) & (r - g > 90) & (r - b > 90)
    lab, n = ndimage.label(mask)
    if n == 0:
        return []
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        w, h = x1 - x0, y1 - y0
        if not (LOGO_MIN_W <= w <= 1.8 * LOGO_W and LOGO_MIN_H <= h <= 1.8 * LOGO_H):
            continue
        ar = w / h
        if not (2.3 <= ar <= 3.9):
            continue
        fill = int((lab[sl] == i).sum()) / (w * h)
        if fill < 0.3 or _plate_fill(arr[sl]) < 0.9:
            continue
        # чем ближе пропорции к эталонным и чем выше в кадре, тем вероятнее это логотип HUD:
        # на втором мониторе попадаются красные блоки покрупнее и пониже
        out.append((abs(ar - LOGO_W / LOGO_H) + y0 / max(1, arr.shape[0]), Box(x0, y0, x1, y1)))
    out.sort(key=lambda t: t[0])
    return [b for _, b in out]


def _find_logo(arr: np.ndarray) -> Box | None:
    c = _logo_candidates(arr)
    return c[0] if c else None


def _logo_fits(logo: Box, box: Box, scale: float) -> bool:
    """Логотип должен лежать в правом верхнем углу предполагаемого окна."""
    return (box.x1 - 220 * scale <= logo.x1 <= box.x1 + 60 * scale
            and box.y0 - 20 <= logo.y0 <= box.y0 + 240 * scale)


def _whole_frame(logo: Box, W: int, H: int) -> "Viewport | None":
    """Кадр целиком — это окно игры (NVIDIA, Steam, полный экран).

    Проверяем эту версию первой: ширину окна нельзя надёжно вычислить из размера
    логотипа. Он меряется с точностью в пару пикселей, а множится на 1920 — ошибка
    в 3 px превращается в 90 px по краю, и окно «уезжает» за границу кадра.

    Масштаб берётся по высоте кадра: интерфейс растёт вместе с ней, а ширина у широких
    мониторов больше при том же интерфейсе. Логотип при этом должен быть того размера,
    какой даёт этот масштаб, — иначе это случайный красный предмет в углу сцены.
    """
    if not (WHOLE_AR[0] <= W / H <= WHOLE_AR[1]):
        return None
    scale = H / REF_H
    if not (0.6 <= logo.w / (LOGO_W * scale) <= 1.6):
        return None
    box = Box(0, 0, W, H)
    if not _logo_fits(logo, box, scale):
        return None
    return Viewport(box=box, scale=scale, logo=logo, frame_w=W, frame_h=H)


def _viewport_from_logo(logo: Box, W: int, H: int) -> "Viewport | None":
    """Окно игры внутри кадра пошире (два монитора)."""
    scale = logo.w / LOGO_W
    cx = int(logo.x1 - CENTER_FROM_LOGO_RIGHT * scale)
    top = max(0, int(logo.y0 - TOP_FROM_LOGO_TOP * scale))
    half = int(HALF_WIDTH * scale)
    left, right = cx - half, cx + half
    # небольшой промах по масштабу не повод отказываться — подрезаем по кадру
    if left < -0.15 * W or right > W * 1.15:
        return None
    box = Box(max(0, left), top, min(W, right), H)
    if box.w < 900 or box.h < 500 or not (1.55 <= box.w / box.h <= 2.05):
        return None
    if not _logo_fits(logo, box, scale):
        return None
    return Viewport(box=box, scale=scale, logo=logo, frame_w=W, frame_h=H)


def detect(im: Image.Image) -> Viewport | None:
    """Найти окно игры. None — якорь не найден, кадр непонятен."""
    W, H = im.size
    arr = np.asarray(im.convert("RGB"))
    cands = _logo_candidates(arr)
    for logo in cands:                      # сперва «весь кадр — это игра»
        vp = _whole_frame(logo, W, H)
        if vp is not None:
            return vp
    for logo in cands:                      # затем окно внутри широкого кадра
        vp = _viewport_from_logo(logo, W, H)
        if vp is not None:
            return vp
    return None
