# -*- coding: utf-8 -*-
"""Раскладка файлов по папкам категорий, обрезка второго монитора, журнал и откат."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import periods
from .config import SERVICE_DIRS, Rules
from .score import Decision, BUCKET_CATEGORY, BUCKET_MANUAL, BUCKET_NO_ACTION

ACTION_RU = {"revive": "реанимация", "heal": "таблетка", "vaccinate": "вакцина",
             "certificate": "медсправка", "call_cancelled": "отменённый_вызов"}
#: подтверждающие плашки, по которым можно назвать кадр, если действия нет
SUPPORTING_RU = {"refused": "отказ-от-лечения", "revive_reward": "награда-за-спасение"}
PLACE_RU = {"gkb1": "ГКБ1", "gkb2": "ГКБ2-Склиф", "asmp": "АСМП", "street": "улица"}


@dataclass
class Move:
    src: str
    dst: str
    kind: str                    # category / manual / no_action / duplicate
    crop: list[int] | None = None
    original_dst: str | None = None   # куда уехал необрезанный исходник
    sidecar: str | None = None        # .txt-записка для ручного разбора
    duplicate_of: str | None = None   # куда положили этот же кадр в прошлый раз
    period: str = ""                  # папка отчётного периода («Не сдано» / «Сдано ...»)
    category_id: str = ""             # категория баллов: по имени папки её не восстановить,
                                      # ведь доп. баллы лежат плоско, без подпапок-категорий
    lunch: bool = False
    orig_category_id: str = ""        # чем кадр был до того, как его отдали в замену:
    orig_points: int = 0              # в недельном он считается как сделанная работа
    hash: str = ""
    points: int = 0
    folder: str = ""


@dataclass
class Plan:
    dest: str
    moves: list[Move] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def summary(self) -> dict:
        by: dict[str, dict] = {}
        for m in self.moves:
            k = (m.folder or SERVICE_DIRS.get(m.kind, m.kind)).replace("\\", "/")
            b = by.setdefault(k, {"files": 0, "points": 0})
            b["files"] += 1
            b["points"] += m.points
        return by


def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", s).strip()


def target_name(facts: dict, d: Decision, ext: str) -> str:
    t = facts.get("time")
    ts = datetime.fromisoformat(t).strftime("%Y-%m-%d_%H-%M") if t else "без-времени"
    what = ACTION_RU.get(facts.get("action") or "", "")
    if not what:
        # действия нет, но подтверждающая плашка говорит, что это было:
        # «отказ» читается куда понятнее, чем «неизвестно»
        what = next((SUPPORTING_RU[t] for t in (facts.get("supporting") or [])
                     if t in SUPPORTING_RU), "неизвестно")
    parts = [ts, what]
    if facts.get("patient_id"):
        parts.append(str(facts["patient_id"]))
    if d.place and d.place in PLACE_RU and facts.get("action") in ("heal", "vaccinate", "certificate"):
        parts.append(PLACE_RU[d.place])
    return _safe("_".join(parts)) + ext


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf = path.stem, path.suffix
    for i in range(2, 1000):
        c = path.with_name(f"{stem}_{i}{suf}")
        if not c.exists():
            return c
    raise RuntimeError(f"не могу подобрать имя для {path}")


def sidecar_text(facts: dict, d: Decision, rules: Rules) -> str:
    a = facts.get("action")
    lines = ["ПОЧЕМУ В РУЧНОМ РАЗБОРЕ:"]
    lines += [f"  • {r}" for r in (d.reasons or ["(причина не указана)"])]
    lines += ["", "ЧТО ПРОГРАММА ВСЁ-ТАКИ РАЗОБРАЛА:",
              f"  действие:     {rules.actions.get(a, {}).get('title', a) if a else '—'}",
              f"  ID пациента:  #{facts.get('patient_id') or '—'}",
              f"  время:        {(facts.get('time') or '—').replace('T', ' ')}  ({facts.get('time_source') or ''})",
              f"  день/ночь:    {d.daypart or '—'}   обед: {'да' if d.lunch else 'нет'}",
              f"  место:        {PLACE_RU.get(d.place, d.place) if d.place else '—'}  ({d.place_source})",
              f"  БЕЗОПАСНО:    {'да' if facts.get('safe_zone') else 'нет'}",
              f"  плашки:       {facts.get('toasts')}",
              f"  подписи карты:{facts.get('map_place_hits') or facts.get('minimap_text')}",
              "", "КУДА ПОЛОЖИТЬ: перетащи файл в нужную папку категории и удали эту записку."]
    return "\n".join(lines)


def already_processed(journal: Path) -> dict[str, str]:
    """Хэши файлов, разобранных в прошлые разы: хэш -> куда положили.

    Источник правды — журнал, но запись засчитывается только если файл на месте.
    Значит после удаления папки или отката всё считается новым, и реестр не надо чистить руками.
    """
    if not journal.exists():
        return {}
    out: dict[str, str] = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        h, dst = rec.get("hash"), rec.get("dst")
        if h and dst and rec.get("kind") != "duplicate" and Path(dst).exists():
            out[h] = dst
    return out


def build_plan(dest: Path, items: list[tuple[dict, Decision]], rules: Rules,
               known: dict[str, str] | None = None) -> Plan:
    """known — хэши, разобранные в прошлые прогоны: такие кадры уходят в дубликаты и не дают баллов."""
    plan = Plan(dest=str(dest))
    seen: dict[str, str] = dict(known or {})
    taken: set[str] = set()

    def reserve(p: Path) -> Path:
        """Свободное имя. Нумеруем «_2», «_3» — цепочка «_x_x_x» нечитаема."""
        q = _unique(p)
        n = 2
        while str(q) in taken:
            q = _unique(p.with_name(f"{p.stem}_{n}{p.suffix}"))
            n += 1
        taken.add(str(q))
        return q

    for facts, d in items:
        src = Path(facts["path"])
        ext = src.suffix.lower()
        h = facts.get("hash", "")
        if h and h in seen:
            dst = reserve(dest / SERVICE_DIRS["duplicates"] / src.name)
            m = Move(str(src), str(dst), "duplicate", hash=h,
                     folder=SERVICE_DIRS["duplicates"], duplicate_of=seen[h])
            plan.moves.append(m)
            continue
        if h:
            seen[h] = str(src)

        crop = facts.get("viewport_box") if facts.get("needs_crop") else None
        if d.bucket == BUCKET_CATEGORY:
            # папка отчётного периода идёт впереди категории: выгружать надо ровно тот
            # период, который сдаёшь, а сданное не должно попасть в следующий отчёт
            per = periods.folder_of(facts.get("time"))
            folder = dest / per / d.folder if per else dest / d.folder
            name = target_name(facts, d, ".png" if crop else ext)
            dst = reserve(folder / name)
            m = Move(str(src), str(dst), "category", crop=crop, hash=h, points=d.points,
                     folder=d.folder, period=per, category_id=d.category_id or "",
                     lunch=bool(d.lunch))
        elif d.bucket == BUCKET_NO_ACTION:
            folder = dest / SERVICE_DIRS["no_action"]
            dst = reserve(folder / (target_name(facts, d, ".png" if crop else ext)))
            m = Move(str(src), str(dst), "no_action", crop=crop, hash=h, folder=SERVICE_DIRS["no_action"])
        else:
            folder = dest / SERVICE_DIRS["manual"]
            dst = reserve(folder / (target_name(facts, d, ".png" if crop else ext)))
            m = Move(str(src), str(dst), "manual", crop=crop, hash=h, folder=SERVICE_DIRS["manual"])
            m.sidecar = str(dst.with_suffix(dst.suffix + ".txt"))
            m._sidecar_text = sidecar_text(facts, d, rules)  # type: ignore[attr-defined]
        if crop:
            m.original_dst = str(reserve(dest / SERVICE_DIRS["originals"] / src.name))
        plan.moves.append(m)
    return plan


def execute(plan: Plan, *, journal: Path, log=print) -> int:
    """Выполнить план. Каждое перемещение сразу пишется в журнал — откат возможен и после сбоя."""
    journal.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    with open(journal, "a", encoding="utf-8") as jf:
        jf.write(json.dumps({"begin": plan.created, "dest": plan.dest}, ensure_ascii=False) + "\n")
        for m in plan.moves:
            src, dst = Path(m.src), Path(m.dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if m.crop:
                im = Image.open(src)
                im.load()
                x0, y0, x1, y1 = m.crop
                im.crop((x0, y0, x1, y1)).save(dst, optimize=False)
                odst = Path(m.original_dst)
                odst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(odst))
            else:
                shutil.move(str(src), str(dst))
            if m.sidecar:
                Path(m.sidecar).write_text(getattr(m, "_sidecar_text", ""), encoding="utf-8")
            rec = asdict(m)
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            jf.flush()
            done += 1
            log(f"  → {m.kind:9} {Path(m.dst).parent.name}/{dst.name}")
    return done


def undo_last(journal: Path, *, log=print) -> int:
    """Откатить последний разбор: вернуть файлы на исходные места."""
    if not journal.exists():
        log("журнала нет — откатывать нечего")
        return 0
    lines = journal.read_text(encoding="utf-8").splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith('{"begin"')]
    if not starts:
        return 0
    block = [json.loads(l) for l in lines[starts[-1] + 1:]]
    n = 0
    for rec in reversed(block):
        src, dst = Path(rec["src"]), Path(rec["dst"])
        restored = False
        try:
            if rec.get("original_dst"):
                o = Path(rec["original_dst"])
                if o.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(o), str(src))
                    restored = True
            elif dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                restored = True
        except OSError as e:
            log(f"  ! не удалось вернуть {dst.name}: {e}")
            continue
        # обрезанную копию и записку убираем отдельно: их блокировка не должна мешать возврату
        for extra in ([dst] if rec.get("original_dst") else []) + ([Path(rec["sidecar"])] if rec.get("sidecar") else []):
            try:
                if extra.exists():
                    extra.unlink()
            except OSError as e:
                log(f"  ! не удалось удалить {extra.name}: {e} — удали вручную")
        n += int(restored)
    # обрезаем журнал до начала последнего блока
    journal.write_text("\n".join(lines[:starts[-1]]) + ("\n" if starts[-1] else ""), encoding="utf-8")
    log(f"откат: возвращено {n} файлов")
    return n


def delete_originals(dest: Path, *, log=print) -> int:
    d = dest / SERVICE_DIRS["originals"]
    if not d.exists():
        return 0
    n = 0
    for p in d.iterdir():
        if p.is_file():
            p.unlink()
            n += 1
    log(f"удалено оригиналов: {n}")
    return n
