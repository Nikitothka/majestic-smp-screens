# -*- coding: utf-8 -*-
"""Окно разбора спорных кадров: картинка + кнопки категорий.

Программа складывает в «Проверить вручную» всё, в чём не уверена. Тащить эти файлы
мышкой по папкам долго, поэтому здесь кадр показывается целиком, плашка действия —
крупно, рядом написано, что удалось прочитать, а категории разложены кнопками.
Нажатие переносит файл, пишет в журнал (значит откат и защита от повторов работают)
и убирает записку .txt.
"""
from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

from PIL import Image, ImageTk

from . import ocr, place, sorter
from .config import SERVICE_DIRS, load_rules
from .extract import extract_cached
from .score import Decision
from .sorter import PLACE_RU
from .viewport import Viewport

VIEW = (940, 500)          # размер большой картинки
STRIP_H = 90               # высота полосы с плашкой действия
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}


def _zone_strip(im: Image.Image, facts: dict) -> Image.Image | None:
    """Кусок кадра с плашкой действия, растянутый до читаемого размера."""
    box = facts.get("viewport_box")
    if not box:
        return None
    vp = Viewport.from_box(box, facts.get("frame_w") or im.width, facts.get("frame_h") or im.height)
    crop = vp.zone_toasts.crop(im)
    if crop.width < 10 or crop.height < 10:
        return None
    k = min(3.0, VIEW[0] / crop.width)
    return crop.resize((int(crop.width * k), int(crop.height * k)), Image.LANCZOS)


def _fit(im: Image.Image, box: tuple[int, int]) -> Image.Image:
    k = min(box[0] / im.width, box[1] / im.height)
    return im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)


class Triage(tk.Toplevel):
    def __init__(self, master, dest: Path, log=print):
        super().__init__(master)
        self.title("Разбор спорных кадров")
        self.geometry("1180x900")
        self.dest = dest
        self.log = log
        self.rules = load_rules()
        self.journal = dest / SERVICE_DIRS["report"] / "journal.jsonl"
        self.files = self._collect()
        self.i = 0
        self.done = 0
        self._photo = None
        self._strip_photo = None
        self._build()
        self._show()

    # ------------------------------------------------------------------ данные
    def _collect(self) -> list[Path]:
        out = []
        for key in ("manual", "no_action"):
            d = self.dest / SERVICE_DIRS[key]
            if d.exists():
                out += [p for p in sorted(d.iterdir())
                        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
        return out

    # --------------------------------------------------------------------- UI
    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill="x", padx=10, pady=(8, 2))
        self.counter = ttk.Label(head, font=("Segoe UI", 11, "bold"))
        self.counter.pack(side="left")
        self.fname = ttk.Label(head, foreground="#666")
        self.fname.pack(side="right")

        self.strip = tk.Label(self, bg="#1e1e1e")
        self.strip.pack(fill="x", padx=10)
        self.canvas = tk.Label(self, bg="#1e1e1e")
        self.canvas.pack(padx=10, pady=6)

        self.info = tk.Text(self, height=5, wrap="word", font=("Consolas", 9),
                            bg="#f6f6f6", relief="flat", state="disabled")
        self.info.pack(fill="x", padx=10)

        box = ttk.LabelFrame(self, text="Куда отнести этот кадр")
        box.pack(fill="x", padx=10, pady=8)
        grid = ttk.Frame(box)
        grid.pack(padx=6, pady=6)
        for n, cat in enumerate(self.rules.categories):
            ttk.Button(grid, text=cat["folder"], width=32,
                       command=lambda c=cat: self._assign(c)).grid(
                row=n // 3, column=n % 3, padx=3, pady=3, sticky="w")

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(bar, text="Не отчётный кадр", command=self._not_report).pack(side="left")
        ttk.Button(bar, text="Пропустить →", command=self._skip).pack(side="left", padx=6)
        ttk.Button(bar, text="Открыть файл", command=self._open).pack(side="left")
        ttk.Button(bar, text="Закрыть", command=self._finish).pack(side="right")
        self.status = ttk.Label(bar, foreground="#777")
        self.status.pack(side="right", padx=12)

    def _show(self):
        if self.i >= len(self.files):
            self._finish()
            return
        p = self.files[self.i]
        self.counter.configure(text=f"Кадр {self.i + 1} из {len(self.files)}")
        self.fname.configure(text=p.name)
        try:
            facts = extract_cached(p)
        except Exception:  # noqa: BLE001
            facts = {"path": str(p)}
        self.facts = facts
        try:
            im = Image.open(p)
            im.load()
            strip = _zone_strip(im, facts)
            if strip is not None:
                self._strip_photo = ImageTk.PhotoImage(_fit(strip, (VIEW[0], STRIP_H)))
                self.strip.configure(image=self._strip_photo)
            else:
                self.strip.configure(image="", text="")
            self._photo = ImageTk.PhotoImage(_fit(im, VIEW))
            self.canvas.configure(image=self._photo)
        except Exception as e:  # noqa: BLE001
            self.canvas.configure(image="", text=f"не открыть картинку: {e!r}")

        a = facts.get("action")
        lines = [
            f"время:    {(facts.get('time') or '—').replace('T', ' ')}   ({facts.get('time_source') or ''})",
            f"действие: {self.rules.actions.get(a, {}).get('title', a) if a else '— не распознано —'}"
            f"    пациент: {facts.get('patient_id') or '—'}",
            f"плашки:   {'; '.join(facts.get('toasts') or []) or '—'}",
            f"карта:    {'; '.join(facts.get('map_place_hits') or facts.get('minimap_text') or []) or '—'}",
        ]
        side = p.with_suffix(p.suffix + ".txt")
        if side.exists():
            # в записке причины идут маркерами «•» — заголовки нам не нужны
            why = [l.strip().lstrip("• ").strip()
                   for l in side.read_text(encoding="utf-8").splitlines() if l.strip().startswith("•")]
            if why:
                lines.append("почему сюда: " + "; ".join(why))
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", "\n".join(lines))
        self.info.configure(state="disabled")
        self.status.configure(text=f"разобрано: {self.done}")

    # ----------------------------------------------------------------- действия
    def _move(self, decision: Decision, note: str):
        p = self.files[self.i]
        facts = dict(self.facts)
        facts["path"] = str(p)
        facts["needs_crop"] = False          # файл уже обрезан при первой раскладке
        facts.setdefault("hash", ocr.file_hash(p))
        if decision.place in place.LABELS:
            place.set_override(facts["hash"], decision.place, "разобрано вручную")
            try:
                place.save_reference(p, decision.place, facts["hash"])
            except Exception:  # noqa: BLE001
                pass
        plan = sorter.build_plan(self.dest, [(facts, decision)], self.rules)
        sorter.execute(plan, journal=self.journal, log=lambda *_: None)
        # выбранное вручную действие надо сохранить в карточку: иначе в ней останется
        # «действия нет», и отчёт с перераскладкой по периодам будут опираться на неправду
        from .scan import SCAN_DIR
        if facts.get("hash"):
            facts["path"] = plan.moves[0].dst
            (SCAN_DIR / f"{facts['hash']}.json").write_text(
                json.dumps(facts, ensure_ascii=False, indent=1), encoding="utf-8")
        side = p.with_suffix(p.suffix + ".txt")
        if side.exists():
            side.unlink()
        self.done += 1
        self.log(f"  {note}: {Path(plan.moves[0].dst).parent.name}/{Path(plan.moves[0].dst).name}")
        self.i += 1
        self._show()

    def _assign(self, cat: dict):
        m = cat.get("match", {})
        d = Decision(bucket="category", category_id=cat["id"], folder=cat["folder"],
                     points=int(cat["points"]), place=m.get("place"),
                     in_hospital=m.get("in_hospital"), daypart=m.get("daypart"),
                     place_source="разобрано вручную")
        # обед удваивается — но только если кадр действительно попал в обеденное окно
        t = self.facts.get("time")
        if t:
            from datetime import datetime
            from .score import in_window
            hhmm = datetime.fromisoformat(t).time()
            if in_window(hhmm, self.rules.times["lunch"]):
                d.lunch = True
                d.folder = f"{self.rules.raw['lunch_folder']}/{d.folder}"
                d.points *= 2
        if not self.facts.get("action"):
            self.facts["action"] = m.get("action")
        self._move(d, "разобрано")

    def _not_report(self):
        self._move(Decision(bucket="no_action"), "не отчётный")

    def _skip(self):
        self.i += 1
        self._show()

    def _open(self):
        import os
        os.startfile(self.files[self.i])  # noqa: S606 — Windows

    def _finish(self):
        if self.done:
            from . import pipeline
            pipeline.rebuild_report(self.dest, log=self.log)
            messagebox.showinfo("Готово", f"Разобрано вручную: {self.done}. Отчёт пересобран.")
        self.destroy()


def open_triage(master, dest: Path, log=print):
    t = Triage(master, dest, log=log)
    if not t.files:
        t.destroy()
        messagebox.showinfo("Пусто", "Спорных кадров нет — разбирать нечего.")
        return None
    return t
