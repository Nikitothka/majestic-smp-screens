# -*- coding: utf-8 -*-
"""Связка: сканирование → место → баллы → план → (раскладка) → отчёт."""
from __future__ import annotations

import json
import shutil
from collections import Counter
import sys
from pathlib import Path

import numpy as np
import yaml

from . import applog, ocr, place, periods, sorter, report, scan as scanmod
from .config import (DEFAULT_DEST, DEFAULT_SOURCES, DATA_DIR, IMAGE_SUFFIXES, PKG_DIR,
                     SERVICE_DIRS, load_rules)
from .extract import extract, extract_cached
from .score import decide, Decision

JOURNAL_NAME = "journal.jsonl"


def _knn_min_conf() -> float:
    cfg = yaml.safe_load((PKG_DIR / "places.yaml").read_text(encoding="utf-8"))
    return float(cfg.get("knn_min_confidence", 0.6))


HOSPITAL_ACTIONS = {"heal", "vaccinate", "certificate"}


def free_gpu(*, log=None) -> None:
    """Отдать видеопамять: без этого игра лагает, пока окно программы открыто."""
    try:
        ocr.release()
        place.release()
        import gc
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        if log:
            log("видеопамять освобождена")
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"не удалось освободить видеопамять: {e!r}")


def strong_map_evidence(f: dict) -> str | None:
    """Место, в котором карта не оставляет сомнений.

    Годится только прямая подпись «N БОЛЬНИЦА» под меткой игрока, и только для работы,
    которая возможна лишь в помещении. Одни ориентиры вокруг (Павелецкая, Кремль) не годятся:
    у реанимации на улице они говорят, где лежал пациент, а не в какой больнице смена.
    """
    if f.get("action") not in HOSPITAL_ACTIONS:
        return None
    mp = f.get("map_place")
    hits = f.get("map_place_hits") or []
    if mp in place.HOSPITAL_LABELS and any("БОЛЬНИЦА" in h for h in hits):
        return mp
    return None


SESSION_GAP_MINUTES = 45


def sessions(facts: list[dict], gap_minutes: int = SESSION_GAP_MINUTES) -> list[list[dict]]:
    """Разбить кадры на рабочие смены по разрыву во времени.

    Смена — это, как правило, работа в одной больнице, поэтому размечать место удобнее
    сменами, а не по одному кадру.
    """
    from datetime import datetime, timedelta
    have = [f for f in facts if f.get("time") and f.get("viewport_box")]
    have.sort(key=lambda f: f["time"])
    out: list[list[dict]] = []
    cur: list[dict] = []
    prev = None
    for f in have:
        t = datetime.fromisoformat(f["time"])
        if prev and (t - prev) > timedelta(minutes=gap_minutes):
            out.append(cur)
            cur = []
        cur.append(f)
        prev = t
    if cur:
        out.append(cur)
    return out


def seed_session_overrides(facts: list[dict], *, log=print) -> int:
    """Если в смене есть кадр с прямой подписью «N БОЛЬНИЦА», распространить его
    на всю работу этой смены, которая возможна только в помещении."""
    n = 0
    for s in sessions(facts):
        labels = {m for m in (strong_map_evidence(f) for f in s) if m}
        if len(labels) != 1:
            continue
        label = labels.pop()
        span = f"{s[0]['time'][:16].replace('T', ' ')}–{s[-1]['time'][11:16]}"
        for f in s:
            if f.get("action") in HOSPITAL_ACTIONS and f.get("hash"):
                place.set_override(f["hash"], label, f"подпись на карте в смене {span}")
                place.save_reference(Path(f["path"]), label, f.get("hash"))
                n += 1
    if n:
        log(f"мест проставлено по подписи карты в смене: {n}")
    return n


def decide_sessions_by_walls(facts: list[dict], walls: dict[str, float],
                             model: "place.WallColorModel", *, log=print) -> dict:
    """Определить больницу для смены по её кадрам с таблетками и распространить на всю смену.

    Внутри смены медик ходит по разным комнатам, поэтому судить по каждому кадру нельзя —
    иначе выходит, что он переезжает между больницами каждые десять минут. А смена целиком —
    это одна больница (если это не так, кадры разойдутся с ответом и уедут в ручной разбор).
    """
    out: dict[str, tuple[str | None, float]] = {}
    if not model.ready:
        return out
    for s in sessions(facts):
        votes: dict[str, int] = {}
        conf = 0.0
        for f in s:
            if f.get("action") not in place.WallColorModel.RELIABLE_ACTIONS:
                continue
            lab, c = model.predict(walls.get(f.get("hash") or ""))
            if lab:
                votes[lab] = votes.get(lab, 0) + 1
                conf = max(conf, c)
        if not votes:
            continue
        best = max(votes, key=votes.get)
        share = votes[best] / sum(votes.values())
        span = f"{s[0]['time'][:16].replace('T', ' ')}–{s[-1]['time'][11:16]}"
        if share < 0.85:
            log(f"смена {span}: кадры с таблетками спорят {votes} — оставляю на ручной разбор")
            continue
        for f in s:
            if f.get("hash") and f.get("action") in HOSPITAL_ACTIONS:
                out[f["hash"]] = (best, conf)
        log(f"смена {span}: {best} по цвету регистратуры ({votes[best]} из {sum(votes.values())} кадров с таблетками)")
    return out


def analyse(sources: list[Path] | None = None, *, log=print, progress=None) -> list[tuple[dict, Decision]]:
    """Все факты + решения по каждому файлу. Ничего не двигает.

    Два прохода: сначала извлечение (дорого, но кэшируется), из него — эталоны мест по
    подписям карты; потом классификация по индексу и решение по баллам (дёшево)."""
    rules = load_rules()
    sources = sources or DEFAULT_SOURCES
    files = list(scanmod.iter_images(sources))
    min_conf = _knn_min_conf()
    scan_dir = scanmod.SCAN_DIR
    scan_dir.mkdir(parents=True, exist_ok=True)

    facts: list[dict] = []
    for i, p in enumerate(files, 1):
        try:
            # чат и панель вызова в баллах не участвуют — не тратим на них OCR
            f = extract(p, want_chat=False).to_dict()
        except Exception as e:  # noqa: BLE001
            f = {"path": str(p), "problems": [f"ошибка: {e!r}"]}
        facts.append(f)
        log(f"[{i}/{len(files)}] {f.get('action') or '-':14} #{f.get('patient_id') or '-':6} "
            f"{(f.get('time') or '')[:16]:16} {Path(p).name[:48]}")
        if progress:
            progress(i, len(files) + len(files) // 4)

    # места, где игра сама подписала больницу, — распространяем на всю смену
    seed_session_overrides(facts, log=log)
    overrides = place.load_overrides()

    # модель «внутри помещения / на улице»: учится на самом корпусе
    usable = [f for f in facts if f.get("viewport_box") and f.get("action")]
    vecs, acts = [], []
    for f in usable:
        try:
            vecs.append(place.embed_file(Path(f["path"]), f.get("hash")))
            acts.append(f["action"])
        except Exception:  # noqa: BLE001
            pass
    indoor = place.IndoorModel.fit(np.stack(vecs), acts, log=log) if vecs else place.IndoorModel()
    if indoor.ready:
        indoor.save()
    else:
        indoor = place.IndoorModel.load()
    idx = place.PlaceIndex.build(log=log)
    idx.save()

    # больница по цвету стен регистратуры — только по кадрам выдачи таблеток,
    # см. WallColorModel: на вакцинациях цвет отделяет комнаты, а не здания
    walls: dict[str, float] = {}
    for f in facts:
        if f.get("hash") and f.get("action") in HOSPITAL_ACTIONS and f.get("viewport_box"):
            try:
                v = place.wall_feature(Path(f["path"]), f["viewport_box"],
                                       (f["frame_w"], f["frame_h"]), f["hash"])
                if v is not None:
                    walls[f["hash"]] = v
            except Exception:  # noqa: BLE001
                pass
    reliable = {f["hash"]: walls[f["hash"]] for f in facts
                if f.get("hash") in walls and f.get("action") in place.WallColorModel.RELIABLE_ACTIONS}
    anchors = {h: o["place"] for h, o in overrides.items() if o.get("place") in place.HOSPITAL_LABELS}
    wall_model = place.WallColorModel.fit(reliable, anchors, log=log)
    if wall_model.ready:
        wall_model.save()
    else:
        loaded = place.WallColorModel.load()
        if loaded.ready:
            wall_model = loaded
    session_place = decide_sessions_by_walls(facts, walls, wall_model, log=log)

    items: list[tuple[dict, Decision]] = []
    for i, f in enumerate(facts, 1):
        guess = None
        ind: tuple[str | None, float] = (None, 0.0)
        a = f.get("action")
        if f.get("viewport_box") and a and rules.place_precision(a) != "none":
            try:
                v = place.embed_file(Path(f["path"]), f.get("hash"))
                ind = indoor.predict(v)
                if len(idx):
                    guess = idx.classify(v, min_conf=min_conf)
                    f["hospital_sim"] = round(idx.nearest_sim(v, place.HOSPITAL_LABELS), 4)
            except Exception as e:  # noqa: BLE001
                f.setdefault("problems", []).append(f"место: {e!r}")
        wall = session_place.get(f.get("hash") or "", (None, 0.0))
        d = decide(f, guess, rules, knn_min_conf=min_conf, indoor=ind,
                   override=overrides.get(f.get("hash") or ""), wall=wall)
        if wall[0]:
            f["wall_color"] = {"place": wall[0], "value": round(walls.get(f["hash"], 0.0), 4)}
        f["indoor"] = {"label": ind[0], "margin": round(ind[1], 4)}
        if guess is not None:
            f["place_guess"] = {"label": guess.label, "confidence": guess.confidence, "top_sim": guess.top_sim}
        items.append((f, d))
        if f.get("hash"):
            (scan_dir / f"{f['hash']}.json").write_text(json.dumps(f, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  {d.bucket:9} {a or '-':10} #{f.get('patient_id') or '-':6} "
            f"{d.folder or ', '.join(d.reasons)[:70]}")
        if progress:
            progress(len(files) + i // 4, len(files) + len(files) // 4)
    return items


def run(sources: list[Path] | None = None, dest: Path = DEFAULT_DEST, *, dry_run: bool = True,
        log=print, progress=None) -> dict:
    applog.cleanup()
    applog.banner("СУХОЙ ПРОГОН" if dry_run else "РАЗБОР")
    log = applog.tee(log)
    with applog.Background():
        return _run(sources, dest, dry_run=dry_run, log=log, progress=progress)


def _run(sources: list[Path] | None, dest: Path, *, dry_run: bool, log, progress) -> dict:
    rules = load_rules()
    items = analyse(sources, log=log, progress=progress)
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    known = sorter.already_processed(journal)
    plan = sorter.build_plan(dest, items, rules, known=known)
    repeats = sum(1 for m in plan.moves if m.kind == "duplicate")
    if repeats:
        log(f"уже разбирались раньше: {repeats} — уйдут в дубликаты и в баллы не пойдут")
    sm = plan.summary()
    log("")
    log("ПЛАН:" if dry_run else "ВЫПОЛНЯЮ:")
    for k, v in sorted(sm.items()):
        log(f"  {k:45} {v['files']:4} файлов  {v['points']:4} б.")
    total = sum(v["points"] for v in sm.values())
    log(f"  ИТОГО {total} баллов, файлов {len(plan.moves)}")
    result = {"items": items, "plan": plan, "summary": sm, "total": total}
    if dry_run:
        free_gpu(log=log)
        (DATA_DIR / "last_plan.json").write_text(
            json.dumps({"dest": plan.dest, "moves": [m.__dict__ for m in plan.moves]},
                       ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return result
    dest.mkdir(parents=True, exist_ok=True)
    n = sorter.execute(plan, journal=journal, log=log)
    log(f"перемещено: {n}")
    # отчёт собираем по всей папке, а не по одному прогону: иначе вчерашняя работа
    # пропадёт из отчёта, хотя файлы на месте
    rebuild_report(dest, log=log)
    result["reports"] = {"dir": dest / SERVICE_DIRS["report"]}
    free_gpu(log=log)
    return result


def recheck_manual(dest: Path = DEFAULT_DEST, *, log=print) -> int:
    """Перепроверить папку ручного разбора и разложить то, что теперь понятно.

    Пригождается, когда правила или разметка мест пополнились: файл, который раньше был
    непонятен, теперь может определиться сам. Ничего не портит — то, что по-прежнему
    непонятно, остаётся на месте.
    """
    applog.banner("ПЕРЕПРОВЕРКА")
    log = applog.tee(log)
    rules = load_rules()
    # смотрим и «без действия»: правила распознавания пополняются, и кадр,
    # в котором раньше не нашлось плашки, теперь может разобраться
    files = []
    for key in ("manual", "no_action"):
        d = dest / SERVICE_DIRS[key]
        if d.exists():
            files += [p for p in sorted(d.iterdir())
                      if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}]
    if not files:
        log("перепроверять нечего")
        return 0
    overrides = place.load_overrides()
    indoor = place.IndoorModel.load()
    wall_model = place.WallColorModel.load()
    idx = place.PlaceIndex.load() or place.PlaceIndex()
    min_conf = _knn_min_conf()

    items, moved = [], 0
    for p in files:
        try:
            f = extract(p, want_chat=False).to_dict()
        except Exception as e:  # noqa: BLE001
            log(f"  {p.name}: ошибка {e!r}")
            continue
        # у обрезанного кадра хэш другой, чем у исходника, — карточку фактов надо записать,
        # иначе пересборка отчёта его не найдёт
        if f.get("hash"):
            (scanmod.SCAN_DIR / f"{f['hash']}.json").write_text(
                json.dumps(f, ensure_ascii=False, indent=1), encoding="utf-8")
        ind, guess, wall = (None, 0.0), None, (None, 0.0)
        a = f.get("action")
        if f.get("viewport_box") and a and rules.place_precision(a) != "none":
            try:
                v = place.embed_file(p, f.get("hash"))
                ind = indoor.predict(v)
                if len(idx):
                    f["hospital_sim"] = round(idx.nearest_sim(v, place.HOSPITAL_LABELS), 4)
                if len(idx):
                    guess = idx.classify(v, min_conf=min_conf)
                if a in HOSPITAL_ACTIONS:
                    wall = wall_model.predict(place.wall_feature(
                        p, f["viewport_box"], (f["frame_w"], f["frame_h"]), f.get("hash")))
            except Exception:  # noqa: BLE001
                pass
        d = decide(f, guess, rules, knn_min_conf=min_conf, indoor=ind,
                   override=overrides.get(f.get("hash") or ""), wall=wall)
        if d.bucket != "category":
            # категорию не нашли, но имя могло стать понятнее: «отказ» вместо «неизвестно»
            want = sorter.target_name(f, d, p.suffix.lower())
            if d.bucket == "no_action" and want != p.name:
                new_p = sorter._unique(p.with_name(want))
                p.rename(new_p)
                _rename_in_journal(dest, p, new_p, log=log)
                log(f"  переименовано: {new_p.name}")
            else:
                log(f"  осталось: {p.name} — {', '.join(d.reasons) or 'непонятно'}")
            continue
        items.append((f, d))

    if not items:
        log("ничего нового не разобралось")
        return 0
    plan = sorter.build_plan(dest, items, rules)
    moved = sorter.execute(plan, journal=dest / SERVICE_DIRS["report"] / JOURNAL_NAME, log=lambda *_: None)
    for m in plan.moves:
        side = Path(m.src + ".txt")
        if side.exists():
            side.unlink()
        log(f"  разобрано: {Path(m.dst).parent.name}/{Path(m.dst).name}")
    log(f"из ручного разбора разложено: {moved}")
    if moved:
        rebuild_report(dest, log=log)
    free_gpu(log=log)
    return moved


def restack_periods(dest: Path = DEFAULT_DEST, *, log=print) -> int:
    """Переложить уже разобранные кадры по папкам отчётных периодов.

    Нужна после отметки о сдаче: кадры, снятые до этого момента, переезжают из «Не сдано»
    в папку сданного отчёта. Категория и имя файла не меняются — меняется только период.
    """
    applog.banner("РАСКЛАДКА ПО ПЕРИОДАМ")
    log = applog.tee(log)
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    if not journal.exists():
        log("журнала нет — перекладывать нечего")
        return 0
    lines = journal.read_text(encoding="utf-8").splitlines()
    out, moved = [], 0
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        dst = rec.get("dst")
        if rec.get("kind") != "category" or not dst or not Path(dst).exists():
            out.append(line)
            continue
        j = scanmod.SCAN_DIR / f"{rec.get('hash')}.json"
        t = None
        if j.exists():
            try:
                t = json.loads(j.read_text(encoding="utf-8")).get("time")
            except (OSError, ValueError):
                t = None
        want = periods.folder_of(t)
        if want == (rec.get("period") or ""):
            out.append(line)
            continue
        old_p = Path(dst)
        new_p = dest / want / rec["folder"] / old_p.name if want else dest / rec["folder"] / old_p.name
        new_p.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(old_p), str(new_p))
        except OSError as e:
            log(f"  ! не переложить {old_p.name}: {e}")
            out.append(line)
            continue
        rec["dst"], rec["period"] = str(new_p), want
        out.append(json.dumps(rec, ensure_ascii=False))
        moved += 1
    journal.write_text(chr(10).join(out) + chr(10), encoding="utf-8")
    # пустые папки прежних периодов убираем, чтобы дерево не мусорилось
    for d in sorted(dest.rglob("*"), key=lambda x: -len(x.parts)):
        if d.is_dir() and not any(d.iterdir()):
            try:
                d.rmdir()
            except OSError:
                pass
    log(f"переложено по периодам: {moved}")
    return moved


def mark_submitted(when, label: str = "", dest: Path = DEFAULT_DEST, *, log=print) -> int:
    """Отметить, что отчёт сдан на этот момент, и разложить кадры по периодам."""
    from datetime import datetime as _dt
    if isinstance(when, str):
        when = _dt.fromisoformat(when.replace(" ", "T"))
    periods.add(when, label)
    log(f"отмечена сдача: {when:%d.%m.%Y %H:%M}" + (f" — {label}" if label else ""))
    n = restack_periods(dest, log=log)
    rebuild_report(dest, log=log)
    return n


SUBS_FILE = DATA_DIR / "substitutions_applied.json"


def apply_substitutions(dest: Path = DEFAULT_DEST, *, log=print) -> int:
    """Вынести кадры, отданные в замену, в отдельную папку «Замены».

    Правила фракции требуют указывать, что на что меняется, поэтому отданные кадры
    должны лежать отдельно, а не в своей категории — иначе проверяющий засчитает их дважды.
    Отдаём самые дешёвые кадры: дорогие (ночные ПМП) выгоднее оставить в основном зачёте.
    """
    applog.banner("ВЫНОС ЗАМЕН")
    log = applog.tee(log)
    rules = load_rules()
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    if not journal.exists():
        log("журнала нет")
        return 0
    items = _items_from_journal(dest, rules)
    if not items:
        log("нечего менять")
        return 0
    prog = report.promotion_progress(items, rules)
    if not prog:
        log("в rules.yaml не задана цель повышения (promotions.target)")
        return 0
    plan = report.substitution_plan(prog, items, rules)
    if not plan:
        log("замены не нужны или нечем менять")
        return 0

    counts = (rules.raw["promotions"].get("counts") or {})
    lines = json.loads(journal.read_text(encoding="utf-8").splitlines()[0]) if False else None
    recs = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines()
            if l.strip().startswith("{")]
    by_dst = {r["dst"]: r for r in recs if r.get("dst")}

    applied, moved = [], 0
    for x in plan:
        quota = {"revive": "pmp", "vaccinate": "vaccines"}.get(x["give_what"])
        ids = counts.get(quota, [])
        # кандидаты: кадры этой категории, самые дешёвые и самые ранние
        cands = [(f, d) for f, d in items
                 if d.bucket == "category" and d.category_id in ids
                 and (f.get("период") or "") in ("", periods.open_folder())
                 and Path(_dst_of(f, by_dst)).exists()]
        cands.sort(key=lambda t: (t[1].points, t[0].get("time") or ""))
        take = cands[: x["give"]]
        if len(take) < x["give"]:
            log(f"  не хватает кадров для замены {x['give']} {x['give_what']}")
            continue
        title = f"{report.plural(x['give'], x['give_what'])} → {report.plural(x['get'], x['get_what'])}"
        for f, d in take:
            src = Path(_dst_of(f, by_dst))
            rec = by_dst[str(src)]
            per = rec.get("period") or ""
            folder = f"{SERVICE_DIRS['swaps']}/{title}"
            new_p = (dest / per / folder / src.name) if per else (dest / folder / src.name)
            new_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(new_p))
            rec["orig_category_id"] = rec.get("orig_category_id") or d.category_id or ""
            rec["orig_points"] = rec.get("orig_points") or rec.get("points", 0)
            rec["dst"], rec["folder"], rec["points"] = str(new_p), folder, 0
            moved += 1
        applied.append({"title": title, **{k: x.get(k) for k in
                                           ("give", "give_what", "get", "get_what",
                                            "cost", "gain", "delta_points")}})
        # журнал дописываем сразу после каждой замены: если дальше что-то сорвётся,
        # файлы на диске и записи о них останутся согласованными
        _save_journal(journal, recs)
        log(f"  замена: {title} — вынесено {x['give']} кадров")

    _save_journal(journal, recs)
    SUBS_FILE.write_text(json.dumps(applied, ensure_ascii=False, indent=1), encoding="utf-8")
    rebuild_report(dest, log=log)
    log(f"в замены вынесено кадров: {moved}")
    return moved


def _save_journal(journal: Path, recs: list[dict]) -> None:
    journal.write_text(chr(10).join(json.dumps(r, ensure_ascii=False) for r in recs) + chr(10),
                       encoding="utf-8")


def _dst_of(facts: dict, by_dst: dict) -> str:
    """Где сейчас лежит кадр. У одного хэша бывает несколько записей в журнале
    (сперва ручной разбор, потом категория) — берём ту, чей файл реально на месте."""
    h = facts.get("hash")
    found = [dst for dst, rec in by_dst.items() if rec.get("hash") == h]
    for dst in found:
        if Path(dst).exists():
            return dst
    return found[0] if found else facts.get("path", "")


def _decision_from_record(rec: dict, rules, dest: Path) -> Decision:
    """Решение по записи журнала.

    Категорию берём из журнала: доп. баллы лежат плоско, и из имени папки не понять,
    какая там была вакцина. Старые записи без категории разбираем по пути, как раньше.
    Кадры, отданные в замену, в зачёт не идут — у них категории нет намеренно.
    """
    dst = rec.get("dst") or ""
    folder = (rec.get("folder") or "").replace("\\", "/")
    if not folder and dst:
        try:
            folder = Path(dst).parent.relative_to(dest).as_posix()
        except ValueError:
            folder = ""
    lunch_folder = rules.raw["lunch_folder"]
    lunch = bool(rec.get("lunch")) or lunch_folder in folder.split("/")
    if SERVICE_DIRS["swaps"] in folder.split("/")[0:2] or folder.startswith(SERVICE_DIRS["swaps"]):
        return Decision(bucket="category", category_id=None, folder=folder, points=0, lunch=False)
    cat = None
    if rec.get("category_id"):
        cat = next((c for c in rules.categories if c["id"] == rec["category_id"]), None)
    if cat is None:
        cat = next((c for c in rules.categories
                    if folder == c["folder"] or folder.endswith("/" + c["folder"])), None)
    return Decision(bucket="category" if cat else rec["kind"],
                    category_id=cat["id"] if cat else None,
                    folder=folder, points=rec.get("points", 0), lunch=lunch)


def _items_from_journal(dest: Path, rules):
    """Пары (факты, решение) по тому, что реально лежит в папке. Общая часть отчёта и замен."""
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    by_dst = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("dst"):
            by_dst[rec["dst"]] = rec
    out = []
    for dst, rec in by_dst.items():
        if not Path(dst).exists():
            continue
        j = scanmod.SCAN_DIR / f"{rec.get('hash')}.json"
        if not j.exists():
            continue
        f = json.loads(j.read_text(encoding="utf-8"))
        f["период"] = rec.get("period") or ""      # чтобы сданное не считалось дважды
        out.append((f, _decision_from_record(rec, rules, dest)))
    return out


def audit(dest: Path = DEFAULT_DEST, *, fix: bool = False, log=print) -> dict:
    """Сверить папку с журналом: нет ли лишних файлов и дублей по содержимому.

    Журнал — источник правды для отчёта, поэтому файл, попавший в категорию мимо журнала
    (перетащили руками, остался от прежнего разбора), в отчёт не войдёт, но уедет
    на Яндекс.Диск как лишний. С fix=True такие файлы регистрируются, а копии того же
    кадра уносятся в дубликаты.
    """
    applog.banner("ПРОВЕРКА ПАПКИ")
    log = applog.tee(log)
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    known: dict[str, dict] = {}
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("dst"):
                known[str(Path(rec["dst"]))] = rec

    # в дубликатах и оригиналах копии лежат законно — они не «лишние» и не «дубли»
    service = {SERVICE_DIRS["report"], SERVICE_DIRS["duplicates"], SERVICE_DIRS["originals"]}
    on_disk = [p for p in dest.rglob("*")
               if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
               and not any(part in service for part in p.relative_to(dest).parts)]

    orphans = [p for p in on_disk if str(p) not in known]
    missing = [r for k, r in known.items() if not Path(k).exists()]
    by_hash: dict[str, list[Path]] = {}
    for p in on_disk:
        by_hash.setdefault(ocr.file_hash(p), []).append(p)
    dupes = {h: ps for h, ps in by_hash.items() if len(ps) > 1}

    # пустые папки остаются после перекладки по периодам и только путают
    empty = 0
    for d in sorted(dest.rglob("*"), key=lambda x: -len(x.parts)):
        if d.is_dir() and d.name not in service and not any(d.iterdir()):
            try:
                d.rmdir()
                empty += 1
            except OSError:
                pass
    if empty:
        log(f"убрано пустых папок: {empty}")

    # кадр в категории без собственной плашки действия — почти всегда второй снимок
    # уже засчитанного случая (плашка догорела, осталась награда). Попадает туда только
    # ручным разбором, поэтому считаем повтором и при fix убираем в дубликаты
    from .extract import best_match, match_contains, _phr
    pats = {k: v for k, v in _phr["toasts"].items() if v}
    no_toast = []
    for dst, rec in known.items():
        if rec.get("kind") != "category" or not Path(dst).exists():
            continue
        if SERVICE_DIRS["swaps"] in str(rec.get("folder", "")):
            continue
        card_p = scanmod.SCAN_DIR / f"{rec.get('hash')}.json"
        try:
            toasts = json.loads(card_p.read_text(encoding="utf-8")).get("toasts") or []
        except (OSError, ValueError):
            continue
        if not any(best_match(t, pats)[1] >= 0.72 or match_contains(t, _phr.get("toasts_contains", {}))
                   for t in toasts):
            no_toast.append(Path(dst))
    for p in no_toast:
        log(f"  без своей плашки (похоже на повтор): {p.relative_to(dest)}")

    log(f"файлов в папке: {len(on_disk)}   в журнале: {len(known)}")
    log(f"лишних (нет в журнале): {len(orphans)}   пропавших: {len(missing)}   "
        f"дублей по содержимому: {len(dupes)}")
    for p in orphans[:20]:
        log(f"  лишний:  {p.relative_to(dest)}")
    for h, ps in list(dupes.items())[:20]:
        log("  дубль:   " + " | ".join(str(x.relative_to(dest)) for x in ps))

    if fix and no_toast:
        d = dest / SERVICE_DIRS["duplicates"]
        d.mkdir(parents=True, exist_ok=True)
        for p in no_toast:
            target = sorter._unique(d / p.name)
            shutil.move(str(p), str(target))
            _rename_in_journal(dest, p, target, log=log)
            _set_kind_in_journal(dest, target, "duplicate")
            log(f"  повтор в дубликаты: {p.name}")
        if not (orphans or dupes):
            rebuild_report(dest, log=log)
    if fix and (orphans or dupes):
        rules = load_rules()
        moved = 0
        for h, ps in dupes.items():
            # оставляем тот экземпляр, который знает журнал; остальные — в дубликаты
            keep = next((p for p in ps if str(p) in known), ps[0])
            for p in ps:
                if p == keep:
                    continue
                d = dest / SERVICE_DIRS["duplicates"]
                d.mkdir(parents=True, exist_ok=True)
                target = sorter._unique(d / p.name)
                shutil.move(str(p), str(target))
                log(f"  копия в дубликаты: {p.relative_to(dest)}")
                moved += 1
                if str(p) in known:
                    known.pop(str(p), None)
        left = [p for p in orphans if p.exists() and str(p) not in known]
        if left:
            log(f"лишних осталось: {len(left)} — их не было в журнале; проверь вручную")
        log(f"убрано копий: {moved}")
        rebuild_report(dest, log=log)
    return {"on_disk": len(on_disk), "orphans": len(orphans),
            "missing": len(missing), "dupes": len(dupes), "no_toast": len(no_toast)}


def reclassify(dest: Path = DEFAULT_DEST, *, dry_run: bool = False, log=print) -> int:
    """Пересчитать решения по текущему, ещё не сданному периоду и переложить изменившиеся.

    Нужна после исправления правил: кадры уже разложены по старой логике, и их надо
    привести к новой. Сданные периоды и папку «Замены» не трогаем никогда.
    """
    applog.banner("ПЕРЕСЧЁТ ПЕРИОДА")
    log = applog.tee(log)
    rules = load_rules()
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    items = _items_from_journal(dest, rules)
    overrides = place.load_overrides()
    indoor = place.IndoorModel.load()
    idx = place.PlaceIndex.load() or place.PlaceIndex()
    min_conf = _knn_min_conf()
    open_p = periods.open_folder()
    by_dst = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("dst"):
            by_dst[rec["dst"]] = rec

    moved = 0
    for f, old in items:
        if (f.get("период") or "") not in ("", open_p):
            continue                                  # сданное не трогаем
        if SERVICE_DIRS["swaps"] in (old.folder or ""):
            continue                                  # отданное в замену — уже учтено
        if old.bucket != "category":
            continue
        # карточка фактов могла остаться от давнего неудачного разбора (так было с кадрами,
        # снятыми после смены разрешения) — перечитываем сам файл
        cur = Path(_dst_of(f, by_dst))
        if cur.exists():
            period = f.get("период")
            f = extract_cached(cur)
            f["период"] = period
        if not f.get("action"):
            continue
        # точное место у таблеток и вакцин решается по всей смене (цвет регистратуры),
        # здесь этого контекста нет — такие кадры не пересчитываем, чтобы не сломать
        if rules.place_precision(f["action"]) != "hospital":
            continue
        dst = _dst_of(f, by_dst)
        src = Path(dst)
        if not src.exists():
            continue
        ind, guess = (None, 0.0), None
        try:
            v = place.embed_file(src, f.get("hash"))
            ind = indoor.predict(v)
            if len(idx):
                guess = idx.classify(v, min_conf=min_conf)
                f["hospital_sim"] = round(idx.nearest_sim(v, place.HOSPITAL_LABELS), 4)
        except Exception as e:  # noqa: BLE001
            log(f"  {src.name}: {e!r}")
            continue
        d = decide(f, guess, rules, knn_min_conf=min_conf, indoor=ind,
                   override=overrides.get(f.get("hash") or ""))
        if d.bucket == "category" and d.folder == old.folder:
            continue
        where = d.folder if d.bucket == "category" else SERVICE_DIRS["manual"]
        log(f"  {src.name}: {old.folder} → {where}"
            + (f"  ({'; '.join(d.reasons)})" if d.reasons else ""))
        if dry_run:
            continue
        f["path"], f["needs_crop"] = str(src), False
        plan = sorter.build_plan(dest, [(f, d)], rules)
        sorter.execute(plan, journal=journal, log=lambda *_: None)
        moved += 1
    if moved:
        rebuild_report(dest, log=log)
    log(f"переложено: {moved}")
    free_gpu()
    return moved


def _rename_in_journal(dest: Path, old_p: Path, new_p: Path, *, log=print) -> None:
    """Поправить путь в журнале, иначе отчёт и защита от повторов потеряют файл."""
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    if not journal.exists():
        return
    out, hit = [], False
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if rec.get("dst") == str(old_p):
            rec["dst"] = str(new_p)
            line = json.dumps(rec, ensure_ascii=False)
            hit = True
        out.append(line)
    if hit:
        journal.write_text(chr(10).join(out) + chr(10), encoding="utf-8")


def _set_kind_in_journal(dest: Path, dst: Path, kind: str) -> None:
    """Пометить запись о файле как повтор: в баллы и отчёт он больше не идёт."""
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    out = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if rec.get("dst") == str(dst):
            rec["kind"], rec["points"], rec["folder"] = kind, 0, SERVICE_DIRS["duplicates"]
            line = json.dumps(rec, ensure_ascii=False)
        out.append(line)
    journal.write_text(chr(10).join(out) + chr(10), encoding="utf-8")


def rebuild_report(dest: Path = DEFAULT_DEST, *, log=print) -> None:
    """Пересобрать отчёты по журналу — после перепроверки или ручной перекладки файлов."""
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    if not journal.exists():
        log("журнала нет — отчёт не пересобрать")
        return
    rules = load_rules()
    by_dst: dict[str, dict] = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if "dst" not in rec:
            continue
        by_dst[rec["dst"]] = rec           # последняя запись про файл — актуальная
    items, moves = [], []
    scan_dir = scanmod.SCAN_DIR
    for dst, rec in by_dst.items():
        if not Path(dst).exists():
            continue
        j = scan_dir / f"{rec.get('hash')}.json"
        if not j.exists():
            continue
        f = json.loads(j.read_text(encoding="utf-8"))
        f["период"] = rec.get("period") or ""      # чтобы сданное не считалось дважды
        items.append((f, _decision_from_record(rec, rules, dest)))
        m = sorter.Move(**{k: v for k, v in rec.items() if k in
                           {"src", "dst", "kind", "crop", "original_dst", "sidecar",
                            "hash", "points", "folder", "period", "category_id", "lunch",
                            "orig_category_id", "orig_points"}})
        m.src = f["path"]
        moves.append(m)
    plan = sorter.Plan(dest=str(dest), moves=moves)
    paths = report.write_all(dest, items, plan, rules)
    log("отчёты пересобраны: " + ", ".join(p.name for p in paths.values()))


def dashboard(dest: Path = DEFAULT_DEST) -> dict:
    """Сводка для окна: прогресс повышения, недели, текущий период и рубежи сдачи."""
    rules = load_rules()
    out = {"prog": None, "weeks": {}, "open": periods.open_folder(),
           "submissions": periods.load(), "total": 0}
    journal = dest / SERVICE_DIRS["report"] / JOURNAL_NAME
    data = dest / SERVICE_DIRS["report"] / "data.json"
    if journal.exists():
        out["prog"] = report.promotion_progress(_items_from_journal(dest, rules), rules)
    if data.exists():
        try:
            d = json.loads(data.read_text(encoding="utf-8"))
            out["weeks"] = report.by_week(d.get("rows", []))
            out["total"] = d.get("summary", {}).get("total", 0)
        except (OSError, ValueError):
            pass
    return out


def undo(dest: Path = DEFAULT_DEST, *, log=print) -> int:
    return sorter.undo_last(dest / SERVICE_DIRS["report"] / JOURNAL_NAME, log=log)


if __name__ == "__main__":
    dry = "--go" not in sys.argv
    srcs = [Path(a) for a in sys.argv[1:] if not a.startswith("--")] or None
    if "--undo" in sys.argv:
        undo()
    elif "--recheck" in sys.argv:
        recheck_manual()
    elif "--report" in sys.argv:
        rebuild_report()
    elif "--reclassify" in sys.argv:
        reclassify(dry_run="--dry" in sys.argv)
    elif "--swaps" in sys.argv:
        apply_substitutions()
    elif "--audit" in sys.argv:
        audit(fix="--fix" in sys.argv)
    elif "--submitted" in sys.argv:
        i = sys.argv.index("--submitted")
        mark_submitted(sys.argv[i + 1], sys.argv[i + 2] if len(sys.argv) > i + 2 else "")
    else:
        run(srcs, dry_run=dry)
