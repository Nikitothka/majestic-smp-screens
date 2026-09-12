# -*- coding: utf-8 -*-
"""Окно программы.

Три вкладки. «Разбор» — откуда брать скриншоты, куда класть, главные кнопки.
«Отчёт» — сколько набрано на повышение и за неделю, отметка о сдаче отчёта.
«Сервис» — всё остальное, что нужно изредка. Лог внизу общий для всех вкладок.

Все личные настройки (папки, цель повышения, запас) сохраняются в data/settings.json
и сливаются с тем, что там уже есть — ничего чужого не затирается.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

from . import periods, pipeline, sorter
from .config import (DEFAULT_DEST, DEFAULT_SOURCES, SERVICE_DIRS, load_rules,
                     load_settings, save_settings)

NO_TARGET = "не считать"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Разбор скриншотов — больничная работа")
        self.geometry("1040x760")
        self.minsize(880, 620)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.sources: list[tuple[tk.BooleanVar, Path]] = []
        self.buttons: list[ttk.Button] = []
        self._build()
        self._load_settings()
        self._refresh_dashboard()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        # при закрытии окна обязательно отдаём видеопамять — иначе игра лагает дальше
        try:
            pipeline.free_gpu()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    # ================================================================== UI
    def _btn(self, parent, text, cmd, **pack):
        b = ttk.Button(parent, text=text, command=cmd)
        b.pack(**(pack or {"side": "left", "padx": (0, 6)}))
        self.buttons.append(b)
        return b

    def _build(self):
        self.status = ttk.Label(self, font=("Segoe UI", 10, "bold"), foreground="#1d5c96")
        self.status.pack(fill="x", padx=12, pady=(8, 2))

        nb = ttk.Notebook(self)
        nb.pack(fill="x", padx=10, pady=4)
        self._build_sort(ttk.Frame(nb))
        self._build_report(ttk.Frame(nb))
        self._build_service(ttk.Frame(nb))
        nb.add(self.tab_sort, text="  Разбор  ")
        nb.add(self.tab_report, text="  Отчёт и повышение  ")
        nb.add(self.tab_service, text="  Сервис  ")
        self.nb = nb

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(4, 4))
        self.log = tk.Text(self, wrap="none", font=("Consolas", 9), state="disabled",
                           bg="#1e1e1e", fg="#ddd", height=12)
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log.tag_configure("bold", foreground="#ffd75f")

    # ---------------------------------------------------------- вкладка «Разбор»
    def _build_sort(self, tab):
        self.tab_sort = tab
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(tab, text="Откуда брать скриншоты")
        top.pack(fill="x", **pad)
        self.src_frame = ttk.Frame(top)
        self.src_frame.pack(fill="x", padx=6, pady=4)
        for s in DEFAULT_SOURCES:
            self._add_source(s, s.exists())
        ttk.Button(top, text="+ Добавить папку…", command=self._browse_source).pack(
            anchor="w", padx=6, pady=(0, 6))

        dst = ttk.LabelFrame(tab, text="Куда раскладывать")
        dst.pack(fill="x", **pad)
        self.dest_var = tk.StringVar(value=str(DEFAULT_DEST))
        row = ttk.Frame(dst)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Entry(row, textvariable=self.dest_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Выбрать…", command=self._browse_dest).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Открыть", command=self._open_dest).pack(side="left", padx=(6, 0))

        self.cpu_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="Видеокарта занята игрой — считать на процессоре "
                                  "(медленнее, но игра не лагает)",
                        variable=self.cpu_var, command=self._save_settings).pack(anchor="w", padx=12)

        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=8, pady=8)
        self._btn(btns, "Показать, что получится", lambda: self._run(dry=True))
        self._btn(btns, "▶  Разобрать", lambda: self._run(dry=False))
        self._btn(btns, "Разобрать спорные…", self._triage)

    # ----------------------------------------------- вкладка «Отчёт и повышение»
    def _build_report(self, tab):
        self.tab_report = tab
        pad = {"padx": 8, "pady": 4}

        pr = ttk.LabelFrame(tab, text="Повышение — считается только то, что ещё не сдано")
        pr.pack(fill="x", **pad)
        row = ttk.Frame(pr)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row, text="Иду на:").pack(side="left")
        ranks = list((load_rules(personal=False).raw.get("promotions", {}).get("ranks") or {}).keys())
        self.target_var = tk.StringVar(value=NO_TARGET)
        cb = ttk.Combobox(row, textvariable=self.target_var, state="readonly", width=12,
                          values=[NO_TARGET] + [r.replace("-", " → ") for r in ranks])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._save_personal())
        ttk.Label(row, text="   держать вакцин сверх нормы про запас:").pack(side="left")
        self.spare_var = tk.IntVar(value=1)
        sp = ttk.Spinbox(row, from_=0, to=10, width=4, textvariable=self.spare_var,
                         command=self._save_personal)
        sp.pack(side="left", padx=6)
        sp.bind("<FocusOut>", lambda _e: self._save_personal())
        self.prog_text = tk.Text(pr, height=7, font=("Consolas", 10), relief="flat",
                                 bg="#f6f6f6", state="disabled")
        self.prog_text.pack(fill="x", padx=6, pady=6)

        wk = ttk.LabelFrame(tab, text="Недельный отчёт — считает всю неделю, включая уже сданное")
        wk.pack(fill="x", **pad)
        self.week_text = tk.Text(wk, height=6, font=("Consolas", 10), relief="flat",
                                 bg="#f6f6f6", state="disabled")
        self.week_text.pack(fill="x", padx=6, pady=6)
        wrow = ttk.Frame(wk)
        wrow.pack(fill="x", padx=6, pady=(0, 6))
        self._btn(wrow, "Собрать папку недели", self._week_folder)
        ttk.Label(wrow, text="все кадры недели в одной папке — её и выгружают",
                  foreground="#555").pack(side="left", padx=4)

        per = ttk.LabelFrame(tab, text="Отчётные периоды — сданное не попадает в следующий отчёт")
        per.pack(fill="x", **pad)
        row = ttk.Frame(per)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row, text="Папка текущего, несданного периода:").pack(side="left")
        self.open_var = tk.StringVar(value=periods.open_folder())
        e = ttk.Entry(row, textvariable=self.open_var, width=24)
        e.pack(side="left", padx=6)
        ttk.Button(row, text="Переименовать", command=self._rename_open).pack(side="left")
        row2 = ttk.Frame(per)
        row2.pack(fill="x", padx=6, pady=(2, 6))
        self._btn(row2, "Отметить сдачу отчёта…", self._mark_submitted)
        self._btn(row2, "Убрать выбранный рубеж", self._remove_submission)
        self.sub_list = tk.Listbox(per, height=4, font=("Consolas", 9))
        self.sub_list.pack(fill="x", padx=6, pady=(0, 6))

        files = ttk.Frame(tab)
        files.pack(fill="x", padx=8, pady=6)
        ttk.Button(files, text="Открыть отчёт", command=lambda: self._open_report("отчёт.txt")).pack(
            side="left", padx=(0, 6))
        ttk.Button(files, text="Открыть недельный", command=self._open_week).pack(side="left", padx=(0, 6))
        ttk.Button(files, text="Таблица xlsx", command=lambda: self._open_report("отчёт.xlsx")).pack(
            side="left", padx=(0, 6))
        ttk.Button(files, text="Обновить", command=self._refresh_dashboard).pack(side="right")

    # ------------------------------------------------------------ вкладка «Сервис»
    def _build_service(self, tab):
        self.tab_service = tab
        grid = [
            ("Разметить места…", self._label,
             "Один раз показать программе, где какая больница, если она ошибается."),
            ("Перепроверить ручной разбор", self._recheck,
             "Прогнать «Проверить вручную» заново — после исправлений часть разложится сама."),
            ("Сверить папку", self._audit,
             "Найти лишние файлы, дубли и повторные снимки одного случая."),
            ("Откатить последний разбор", self._undo,
             "Вернуть файлы последнего разбора на исходные места."),
            ("Удалить оригиналы", self._delete_originals,
             "Снести необрезанные кадры с двумя мониторами."),
            ("Освободить видеопамять", self._free,
             "Выгрузить модели, если игра лагает. Делается и само после разбора."),
            ("Открыть лог", self._open_log, "Всё, что программа писала, по дням."),
        ]
        for text, cmd, hint in grid:
            row = ttk.Frame(tab)
            row.pack(fill="x", padx=8, pady=3)
            b = ttk.Button(row, text=text, command=cmd, width=30)
            b.pack(side="left")
            self.buttons.append(b)
            ttk.Label(row, text=hint, foreground="#555").pack(side="left", padx=10)

    # ============================================================ источники
    def _add_source(self, path: Path, enabled: bool = True):
        var = tk.BooleanVar(value=enabled)
        ttk.Checkbutton(self.src_frame, text=str(path) + ("" if path.exists() else "   (папки нет)"),
                        variable=var, command=self._save_settings).pack(anchor="w")
        self.sources.append((var, path))

    def _browse_source(self):
        d = filedialog.askdirectory(title="Папка со скриншотами")
        if d:
            self._add_source(Path(d))
            self._save_settings()

    def _browse_dest(self):
        d = filedialog.askdirectory(title="Куда раскладывать")
        if d:
            self.dest_var.set(d)
            self._save_settings()
            self._refresh_dashboard()

    def _open_dest(self):
        d = Path(self.dest_var.get())
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)  # noqa: S606 — Windows

    # ============================================================ настройки
    def _load_settings(self):
        s = load_settings()
        self.dest_var.set(s.get("dest", self.dest_var.get()))
        self.cpu_var.set(bool(s.get("cpu", False)))
        known = {str(p) for _, p in self.sources}
        for extra in s.get("extra_sources", []):
            if extra not in known:
                self._add_source(Path(extra))
        for var, p in self.sources:
            if str(p) in s.get("disabled", []):
                var.set(False)
        t = s.get("target_rank") or ""
        self.target_var.set(t.replace("-", " → ") if t else NO_TARGET)
        self.spare_var.set(int(s.get("spare_vaccines", 1)))

    def _save_settings(self):
        defaults = {str(p) for p in DEFAULT_SOURCES}
        save_settings({
            "dest": self.dest_var.get(),
            "cpu": self.cpu_var.get(),
            "extra_sources": [str(p) for _, p in self.sources if str(p) not in defaults],
            "disabled": [str(p) for v, p in self.sources if not v.get()],
        })

    def _save_personal(self):
        t = self.target_var.get()
        try:
            spare = max(0, int(self.spare_var.get()))
        except (ValueError, tk.TclError):
            spare = 1
        save_settings({"target_rank": "" if t == NO_TARGET else t.replace(" → ", "-"),
                       "spare_vaccines": spare})
        self._say(f"цель повышения: {t}, запас вакцин: {spare}")
        self._work(lambda: pipeline.rebuild_report(Path(self.dest_var.get()), log=self._say),
                   after=self._refresh_dashboard)

    # ============================================================== сводка
    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _refresh_dashboard(self):
        try:
            d = pipeline.dashboard(Path(self.dest_var.get()))
        except Exception as e:  # noqa: BLE001
            self.status.configure(text=f"сводка недоступна: {e!r}")
            return
        from .report import plural
        RU = {"pmp": "ПМП", "vaccines": "вакцины", "points": "баллы"}
        prog = d["prog"]
        head = []
        if prog:
            lines = [f"{RU[q]:9} {prog['have'][q]:5} из {prog['need'][q]:4}   "
                     + ("✔ хватает" if not prog["gaps"][q] else f"не хватает {prog['gaps'][q]}")
                     for q in ("pmp", "vaccines", "points")]
            if prog.get("exam"):
                lines.append(f"экзамен: {prog['exam']}")
            for sw in prog.get("swaps") or []:
                lines.append(f"применена замена: {sw['title']}")
            self._set_text(self.prog_text, "\n".join(lines))
            head.append(f"Повышение {prog['target'].replace('-', '→')}: " + (
                "всё закрыто" if prog["done"] else
                ", ".join(f"{RU[q]} {prog['have'][q]}/{prog['need'][q]}" for q in ("pmp", "vaccines", "points"))))
        else:
            self._set_text(self.prog_text, "Цель повышения не выбрана — выбери ранг выше.")
        weeks = d["weeks"]
        if weeks:
            wl = []
            for w in list(weeks.values())[-3:]:
                a = datetime.fromisoformat(w["с"]).date()
                b = datetime.fromisoformat(w["по"]).date()
                wl.append(f"неделя {a:%d.%m}–{b:%d.%m}:  {plural(w['баллов'], 'points')},  "
                          f"{plural(w['файлов'], 'shots')}")
            self._set_text(self.week_text, "\n".join(wl))
            last = list(weeks.values())[-1]
            head.append(f"Неделя: {last['баллов']} б.")
        else:
            self._set_text(self.week_text, "Пока ничего не разобрано.")
        self.status.configure(text="   |   ".join(head) or "Готов к разбору")
        self.open_var.set(d["open"])
        self.sub_list.delete(0, "end")
        for s in reversed(d["submissions"]):
            t = datetime.fromisoformat(s["when"])
            self.sub_list.insert("end", f"{t:%d.%m.%Y %H:%M}   {s.get('label') or ''}")
        self._subs = list(reversed(d["submissions"]))

    def _open_report(self, name):
        p = Path(self.dest_var.get()) / SERVICE_DIRS["report"] / name
        if p.exists():
            os.startfile(p)  # noqa: S606
        else:
            messagebox.showinfo("Отчёт", "Отчёта ещё нет — сначала разбери скриншоты.")

    def _week_folder(self):
        """Собрать неделю в одну папку: сданное и несданное вместе, по категориям."""
        dest = Path(self.dest_var.get())

        def body():
            res = pipeline.week_folder(dest, log=self._say)
            self.q.put(("week_done", res))

        self._work(body, after=self._refresh_dashboard)

    def _open_week(self):
        rdir = Path(self.dest_var.get()) / SERVICE_DIRS["report"]
        weeks = sorted(rdir.glob("неделя *.txt")) if rdir.exists() else []
        if weeks:
            os.startfile(weeks[-1])  # noqa: S606
        else:
            messagebox.showinfo("Недельный", "Недельного отчёта ещё нет.")

    # ============================================================ периоды
    def _rename_open(self):
        name = self.open_var.get().strip()
        if any(c in name for c in '<>:"/\\|?*'):
            messagebox.showwarning("Имя папки", 'Нельзя использовать символы < > : " / \\ | ? *')
            return
        periods.set_open_label(name)
        dest = Path(self.dest_var.get())
        self._work(lambda: (pipeline.restack_periods(dest, log=self._say),
                            pipeline.rebuild_report(dest, log=self._say)),
                   after=self._refresh_dashboard)

    def _mark_submitted(self):
        dlg = tk.Toplevel(self)
        dlg.title("Отчёт сдан")
        dlg.transient(self)
        dlg.grab_set()
        now = datetime.now()
        ttk.Label(dlg, text="Всё, что снято ДО этого момента, считается сданным и уедет\n"
                            "в отдельную папку. Новые скриншоты пойдут в следующий отчёт.",
                  justify="left").grid(row=0, column=0, columnspan=2, padx=12, pady=(12, 8), sticky="w")
        ttk.Label(dlg, text="Дата (дд.мм.гггг):").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        dv = tk.StringVar(value=now.strftime("%d.%m.%Y"))
        ttk.Entry(dlg, textvariable=dv, width=14).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(dlg, text="Время (чч:мм):").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        tv = tk.StringVar(value=now.strftime("%H:%M"))
        ttk.Entry(dlg, textvariable=tv, width=8).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(dlg, text="Какой отчёт (например 6-7):").grid(row=3, column=0, sticky="e", padx=8, pady=4)
        lv = tk.StringVar(value=self.open_var.get() if self.open_var.get() != periods.OPEN_FOLDER else "")
        ttk.Entry(dlg, textvariable=lv, width=20).grid(row=3, column=1, sticky="w", pady=4)

        def ok():
            try:
                when = datetime.strptime(f"{dv.get().strip()} {tv.get().strip()}", "%d.%m.%Y %H:%M")
            except ValueError:
                messagebox.showwarning("Дата", "Не понял дату или время. Пример: 11.09.2026 и 13:40", parent=dlg)
                return
            label = lv.get().strip()
            dlg.destroy()
            periods.set_open_label("")          # следующий период начинается как «Не сдано»
            dest = Path(self.dest_var.get())
            self._work(lambda: pipeline.mark_submitted(when, label, dest, log=self._say),
                       after=self._refresh_dashboard)

        b = ttk.Frame(dlg)
        b.grid(row=4, column=0, columnspan=2, pady=12)
        ttk.Button(b, text="Отметить", command=ok).pack(side="left", padx=6)
        ttk.Button(b, text="Отмена", command=dlg.destroy).pack(side="left", padx=6)

    def _remove_submission(self):
        sel = self.sub_list.curselection()
        if not sel:
            messagebox.showinfo("Рубеж", "Выбери рубеж в списке.")
            return
        s = self._subs[sel[0]]
        if not messagebox.askyesno("Убрать рубеж",
                                   f"Убрать отметку о сдаче {s['when'].replace('T', ' ')}?\n"
                                   "Кадры из этой папки вернутся в соседний период."):
            return
        periods.remove(s["when"])
        dest = Path(self.dest_var.get())
        self._work(lambda: (pipeline.restack_periods(dest, log=self._say),
                            pipeline.rebuild_report(dest, log=self._say)),
                   after=self._refresh_dashboard)

    # ============================================================== работа
    def _selected_sources(self) -> list[Path]:
        return [p for v, p in self.sources if v.get() and p.exists()]

    def _say(self, text: str, tag: str | None = None):
        self.q.put(("log", text, tag))

    def _pump(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "log":
                    text, tag = rest
                    self.log.configure(state="normal")
                    self.log.insert("end", str(text) + "\n", tag or ())
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "progress":
                    i, n = rest
                    self.progress.configure(maximum=max(1, n), value=i)
                elif kind == "week_done":
                    res = rest[0]
                    if res["files"] and messagebox.askyesno(
                            "Папка недели",
                            f"Собрано {res['files']} скриншотов на {res['points']} баллов.\n"
                            f"{res['folder']}\n\nОткрыть папку?"):
                        os.startfile(res["folder"])  # noqa: S606
                elif kind == "done":
                    self._set_busy(False)
                    fn = rest[0]
                    if fn:
                        fn()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _set_busy(self, busy: bool):
        self.busy = busy
        for b in self.buttons:
            b.configure(state="disabled" if busy else "normal")

    def _work(self, fn, after=None):
        if self.busy:
            return
        os.environ["MEDREPORT_DEVICE"] = "cpu" if self.cpu_var.get() else ""
        self._set_busy(True)

        def body():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                self._say("ОШИБКА: " + repr(e), "bold")
                self._say(traceback.format_exc())
            finally:
                self.q.put(("done", after))
        threading.Thread(target=body, daemon=True).start()

    def _run(self, dry: bool):
        srcs = self._selected_sources()
        if not srcs:
            messagebox.showwarning("Нет источников", "Отметь хотя бы одну папку со скриншотами.")
            return
        dest = Path(self.dest_var.get())
        if not dry and not messagebox.askyesno(
                "Разобрать", f"Файлы будут ПЕРЕМЕЩЕНЫ из исходных папок в\n{dest}\n\n"
                             "Откат возможен на вкладке «Сервис». Продолжить?"):
            return
        self._save_settings()
        self._say(("СУХОЙ ПРОГОН — ничего не двигаю" if dry else "РАЗБОР — файлы перемещаются"), "bold")
        self.progress.configure(value=0)

        def job():
            res = pipeline.run(srcs, dest, dry_run=dry, log=self._say,
                               progress=lambda i, n: self.q.put(("progress", i, n)))
            self._say(f"ИТОГО по папке: {res['total']} баллов", "bold")

        def after():
            self._refresh_dashboard()
            if not dry:
                self._open_dest()

        self._work(job, after=after)

    def _label(self):
        from .labeler import open_labeler, collect_sessions
        srcs = self._selected_sources()
        self._say("Собираю смены (первый раз считает эмбеддинги, потом мгновенно)…", "bold")
        holder = {}

        def job():
            holder["sessions"] = collect_sessions(srcs, log=self._say)

        def show():
            open_labeler(self, srcs, on_done=lambda: self._say(
                "Разметка сохранена. Жми «Показать, что получится» — места подставятся.", "bold"))

        self._work(job, after=show)

    def _open_log(self):
        from . import applog
        p = applog.log_path()
        if not p.exists():
            applog.write("лог открыт до первого запуска")
        os.startfile(p)  # noqa: S606 — Windows

    def _free(self):
        pipeline.free_gpu(log=self._say)

    def _triage(self):
        from .triage import open_triage
        open_triage(self, Path(self.dest_var.get()), log=self._say)

    def _recheck(self):
        dest = Path(self.dest_var.get())
        self._say("Перепроверяю «Проверить вручную» и «без действия»…", "bold")
        self._work(lambda: pipeline.recheck_manual(dest, log=self._say), after=self._refresh_dashboard)

    def _audit(self):
        dest = Path(self.dest_var.get())
        self._say("Сверяю папку с журналом…", "bold")

        def job():
            r = pipeline.audit(dest, log=self._say)
            if r.get("no_toast") or r.get("dupes") or r.get("orphans"):
                self.q.put(("log", "Нашлись проблемы — жми «Сверить папку» ещё раз с исправлением "
                                   "(будет вопрос).", "bold"))
                holder["fix"] = True

        holder = {}

        def after():
            if holder.get("fix") and messagebox.askyesno(
                    "Исправить", "Убрать повторы и копии в дубликаты?"):
                self._work(lambda: pipeline.audit(dest, fix=True, log=self._say),
                           after=self._refresh_dashboard)
            else:
                self._refresh_dashboard()

        self._work(job, after=after)

    def _undo(self):
        dest = Path(self.dest_var.get())
        if not messagebox.askyesno("Откат", "Вернуть файлы последнего разбора на исходные места?"):
            return
        self._work(lambda: pipeline.undo(dest, log=self._say), after=self._refresh_dashboard)

    def _delete_originals(self):
        dest = Path(self.dest_var.get())
        d = dest / SERVICE_DIRS["originals"]
        n = sum(1 for _ in d.iterdir()) if d.exists() else 0
        if n == 0:
            messagebox.showinfo("Оригиналы", "Удалять нечего.")
            return
        if messagebox.askyesno("Удалить оригиналы", f"Удалить {n} необрезанных оригиналов безвозвратно?\n"
                                                    "После этого откат обрезанных файлов станет невозможен."):
            self._work(lambda: sorter.delete_originals(dest, log=self._say))


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
