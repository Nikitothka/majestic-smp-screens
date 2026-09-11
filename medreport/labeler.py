# -*- coding: utf-8 -*-
"""Разметка мест по сменам — один раз.

Какая именно больница, по картинке не угадать: ГКБ 1 и Склиф похожи, а ошибка стоит баллов.
Зато смена (несколько часов подряд без больших перерывов) — это почти всегда одна больница,
поэтому спрашиваем не про каждый кадр, а про смену: 10–15 вопросов на весь архив.

Ответ записывается прямо по хэшу файла (data/place_overrides.json) — это точное знание,
а не догадка классификатора. Заодно кадры сохраняются как эталоны интерьера, чтобы
будущие скрины определялись сами.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import numpy as np
from PIL import Image, ImageTk

from . import place
from .config import DEFAULT_SOURCES

# Названия — игровые, с ориентирами с карты: так вопрос объективный, а не «как я привык называть».
# Во что это превращается в баллах, записано в rules.yaml.
LABEL_BUTTONS = [("1 Больница\n(Павелецкая, ТЭЦ)", "gkb1"),
                 ("2 Больница «Склиф»\n(ЦУМ, Кремль)", "gkb2"),
                 ("Мед.отсек АСМП\n(в машине)", "asmp")]
PLACE_RU = {"gkb1": "1 Больница", "gkb2": "2 Больница (Склиф)", "asmp": "АСМП"}
THUMB = (296, 176)
GRID = (4, 2)


def _sheet(paths: list[Path]) -> Image.Image:
    cols, rows = GRID
    W, H = THUMB
    sheet = Image.new("RGB", (cols * (W + 6) + 6, rows * (H + 6) + 6), (32, 32, 32))
    for i, p in enumerate(paths[: cols * rows]):
        try:
            im = Image.open(p)
            im.load()
            sc = place.scene_crop(im).resize(THUMB, Image.LANCZOS)
        except Exception:  # noqa: BLE001
            sc = Image.new("RGB", THUMB, (70, 0, 0))
        sheet.paste(sc, (6 + (i % cols) * (W + 6), 6 + (i // cols) * (H + 6)))
    return sheet


def _spread(items: list[dict], n: int) -> list[Path]:
    """Взять n кадров, равномерно растянув по смене, — чтобы увидеть её целиком."""
    if len(items) <= n:
        return [Path(f["path"]) for f in items]
    step = len(items) / n
    return [Path(items[int(i * step)]["path"]) for i in range(n)]


class Labeler(tk.Toplevel):
    def __init__(self, master, sessions: list[list[dict]], on_done=None):
        super().__init__(master)
        self.title("Разметка мест — в какой больнице была смена?")
        self.on_done = on_done
        self.overrides = place.load_overrides()
        self.queue = [s for s in (self._hospital_work(s) for s in sessions) if s]
        self.done = 0
        self._build()
        self._show()
        self.transient(master)
        self.grab_set()

    @staticmethod
    def _hospital_work(session: list[dict]) -> list[dict]:
        return [f for f in session if f.get("action") in place.IndoorModel.INSIDE_ACTIONS and f.get("hash")]

    # ------------------------------------------------------------------ UI
    def _build(self):
        self.geometry("1240x850")
        self.info = ttk.Label(self, font=("Segoe UI", 12, "bold"))
        self.info.pack(pady=(10, 2))
        self.hint = ttk.Label(self, foreground="#0a6")
        self.hint.pack()
        self.canvas = tk.Label(self, background="#202020")
        self.canvas.pack(pady=(6, 2))
        self.refs_label = ttk.Label(self, foreground="#555")
        self.refs_label.pack()
        self.refs = tk.Label(self, background="#202020")
        self.refs.pack(pady=(2, 4))
        bar = ttk.Frame(self)
        bar.pack(pady=4)
        for text, lab in LABEL_BUTTONS:
            tk.Button(bar, text=text, width=22, height=2, command=lambda l=lab: self._answer(l),
                      font=("Segoe UI", 9)).pack(side="left", padx=5)
        ttk.Button(bar, text="Я менял больницу — разбить пополам", command=self._split).pack(side="left", padx=14)
        ttk.Button(bar, text="Не помню — пропустить", command=self._skip).pack(side="left", padx=5)
        self.status = ttk.Label(self, foreground="#777")
        self.status.pack(pady=(6, 10))

    def _show(self):
        if not self.queue:
            self._finish()
            return
        items = self.queue[0]
        self.photo = ImageTk.PhotoImage(_sheet(_spread(items, GRID[0] * GRID[1])))
        self.canvas.configure(image=self.photo)
        t0 = items[0]["time"][:16].replace("T", " ")
        t1 = items[-1]["time"][11:16]
        acts = {}
        for f in items:
            acts[f["action"]] = acts.get(f["action"], 0) + 1
        what = ", ".join(f"{k}: {v}" for k, v in acts.items())
        self.info.configure(text=f"Смена {t0} – {t1}   ({len(items)} кадров: {what})")
        known = {self.overrides[f["hash"]]["place"] for f in items
                 if f["hash"] in self.overrides}
        self.hint.configure(
            text=(f"Игра подписала на карте: {', '.join(PLACE_RU.get(k, k) for k in known)}" if known
                  else "Подписи на карте в этой смене нет — смотри на интерьер"))
        self.status.configure(text=f"Осталось смен: {len(self.queue)}   ·   размечено кадров: {self.done}")
        self._show_refs()

    def _show_refs(self):
        """Полоска уже подтверждённых эталонов — чтобы было с чем сравнить."""
        strip, names = [], []
        for lab in ("gkb1", "gkb2", "asmp"):
            d = place.PLACE_REFS_DIR / lab
            files = sorted(d.glob("*.jpg"))[:3] if d.exists() else []
            for f in files:
                strip.append(f)
            if files:
                names.append(f"{PLACE_RU[lab]} — {len(files)} шт")
        if not strip:
            self.refs_label.configure(text="Эталонов пока нет — первую смену определи сам, дальше будет с чем сравнивать")
            self.refs.configure(image="")
            return
        self.refs_label.configure(text="Уже известно (для сравнения): " + ",   ".join(names))
        W, H = 176, 104
        img = Image.new("RGB", (len(strip) * (W + 4) + 4, H + 8), (32, 32, 32))
        for i, f in enumerate(strip):
            try:
                img.paste(Image.open(f).resize((W, H), Image.LANCZOS), (4 + i * (W + 4), 4))
            except Exception:  # noqa: BLE001
                pass
        self._refs_photo = ImageTk.PhotoImage(img)
        self.refs.configure(image=self._refs_photo)

    # -------------------------------------------------------------- действия
    def _answer(self, label: str):
        items = self.queue.pop(0)
        span = f"{items[0]['time'][:16].replace('T', ' ')}–{items[-1]['time'][11:16]}"
        for f in items:
            place.set_override(f["hash"], label, f"разметка смены {span}")
            try:
                place.save_reference(Path(f["path"]), label, f["hash"])
            except Exception:  # noqa: BLE001
                pass
            self.done += 1
        self.overrides = place.load_overrides()
        self._show()

    def _split(self):
        items = self.queue.pop(0)
        if len(items) < 4:
            self.queue.insert(0, items)
            messagebox.showinfo("Мало кадров", "В этой смене слишком мало кадров, чтобы делить.")
            return
        mid = len(items) // 2
        self.queue = [items[:mid], items[mid:]] + self.queue
        self._show()

    def _skip(self):
        self.queue.pop(0)
        self._show()

    def _finish(self):
        idx = place.PlaceIndex.build(log=lambda *_: None)
        idx.save()
        messagebox.showinfo("Готово", f"Размечено кадров: {self.done}.\n"
                                      f"Эталонов интерьера: {len(idx)} — новые скрины будут определяться сами.")
        self.destroy()
        if self.on_done:
            self.on_done()


def collect_sessions(sources: list[Path] | None = None, *, log=print) -> list[list[dict]]:
    """Факты по всем источникам (из кэша) + разбивка на смены."""
    from .extract import extract_cached
    from .pipeline import sessions
    from .scan import iter_images
    facts = []
    for p in iter_images(sources or DEFAULT_SOURCES):
        try:
            facts.append(extract_cached(p))
        except Exception:  # noqa: BLE001
            continue
    return sessions(facts)


def open_labeler(master, sources: list[Path] | None = None, on_done=None):
    ss = collect_sessions(sources)
    todo = [s for s in ss if any(f.get("action") in place.IndoorModel.INSIDE_ACTIONS for f in s)]
    if not todo:
        messagebox.showinfo("Нечего размечать", "Не нашлось смен с лечением или вакцинацией.")
        return None
    return Labeler(master, todo, on_done=on_done)
