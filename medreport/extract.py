# -*- coding: utf-8 -*-
"""Из скриншота — факты: действие, ID пациента, время, безопасная зона, ориентиры."""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import yaml
from PIL import Image

from . import ocr, viewport
from .config import load_phrases, PKG_DIR

ID_RE = re.compile(r"#\s?(\d{3,6})")
# OCR путает цифры с похожими буквами: «#З0323» вместо «#30323», «#4О5» вместо «#405».
# В номере игрока букв не бывает, поэтому после решётки такие символы смело чиним.
_DIGIT_LOOKALIKE = str.maketrans({
    "З": "3", "з": "3", "Э": "3", "э": "3",
    "О": "0", "о": "0", "O": "0", "o": "0", "Q": "0", "D": "0",
    "б": "6", "Б": "6", "G": "6",
    "l": "1", "I": "1", "|": "1", "і": "1", "!": "1",
    "Ч": "4", "ч": "4", "A": "4",
    "S": "5", "s": "5", "Ѕ": "5",
    "g": "9", "q": "9",
    "Z": "2", "z": "2",
    "B": "8", "В": "8",
    "T": "7", "Т": "7",
})
_ID_LOOSE = re.compile(r"#\s?([0-9ЗзЭэОоOoQDбБGlI|і!ЧчASsЅgqZzBВTТ]{3,6})(?![\wА-Яа-я])")


# «Вы реанимировали Павел Ивлев» — игра иногда пишет имя вместо номера.
# Берём хвост из одного-трёх слов с большой буквы.
_NAME_TAIL = re.compile(r"((?:[А-ЯЁ][а-яё]+[\s-]+){0,2}[А-ЯЁ][а-яё]+)\s*[!.]?\s*$")


def find_patient(text: str) -> str | None:
    """Кого лечили: номер вида 12345 либо имя, если игра написала имя."""
    pid = find_patient_id(text)
    if pid:
        return pid
    m = _NAME_TAIL.search(text.strip())
    if m:
        name = " ".join(m.group(1).split())
        if name.split()[0] not in _NOT_A_NAME and len(name) >= 4:
            return name
    return None


def find_patient_id(text: str) -> str | None:
    """Номер игрока из строки: сперва как есть, потом с починкой букв-двойников."""
    m = ID_RE.search(text)
    if m:
        return m.group(1)
    m = _ID_LOOSE.search(text)
    if m:
        fixed = m.group(1).translate(_DIGIT_LOOKALIKE)
        if fixed.isdigit():
            return fixed
    return None
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*[:.;]\s*([0-5]\d)(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(\d{2})\s*\.\s*(\d{2})\s*\.\s*(20\d{2})(?!\d)")
# время в имени файла: NVIDIA «... 2026.09.07 - 15.24.16.67.png», Steam «20260902121357_1.jpg»
FN_NVIDIA = re.compile(r"(20\d{2})\.(\d{2})\.(\d{2}) - (\d{2})\.(\d{2})\.(\d{2})")
FN_STEAM = re.compile(r"(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})_\d+")

_phr = load_phrases()
_places = yaml.safe_load((PKG_DIR / "places.yaml").read_text(encoding="utf-8"))


_NOT_A_NAME = {"Вы", "Смерть", "Ранение", "Игрока", "Помощь", "Этот", "Вызов"}

#: «Иван Петров» в начале или в конце строки — там, где обычно стоит номер игрока
_NAME_HEAD = re.compile(r"^((?:[А-ЯЁ][а-яё]+[\s-]+){0,2}[А-ЯЁ][а-яё]+)(?=\s)")
_NAME_END = re.compile(r"((?:[А-ЯЁ][а-яё]+[\s-]+){0,2}[А-ЯЁ][а-яё]+)\s*[!.]?\s*$")


def mask_names(text: str) -> str:
    """Заменить имя игрока на такой же знак, как номер.

    Игра пишет то «Вы вылечили #12345», то «Вы вылечили Федор Щукин». Без этой замены
    строка с именем совпадает с образцом всего на 0.6 при пороге 0.72 — и кадр теряется.
    """
    def sub(m):
        name = m.group(1)
        return "#0" if name.split()[0] not in _NOT_A_NAME and len(name) >= 4 else name

    text = _NAME_HEAD.sub(sub, text)
    return _NAME_END.sub(sub, text)


def norm(s: str) -> str:
    s = mask_names(s)
    s = s.lower().replace("ё", "е")
    s = ID_RE.sub("#{id}", s)
    s = re.sub(r"[^a-zа-я0-9#{}₽ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def match_contains(text: str, catalogue: dict) -> str | None:
    """Тег, все куски которого встречаются в строке. Для плашек с изменчивой серединой."""
    t = norm(text)
    for tag, variants in (catalogue or {}).items():
        for parts in variants:
            if all(norm(p) in t for p in parts):
                return tag
    return None


def best_match(text: str, patterns: dict) -> tuple[str | None, float]:
    """Лучшее совпадение текста с каталогом {тег: фраза | [фразы]}."""
    best, score = None, 0.0
    for tag, pats in patterns.items():
        for pat in ([pats] if isinstance(pats, str) else pats):
            r = similarity(text, pat)
            if r > score:
                best, score = tag, r
    return best, score


def filename_time(path: Path) -> datetime | None:
    for rx in (FN_NVIDIA, FN_STEAM):
        m = rx.search(path.name)
        if m:
            y, mo, d, hh, mm, ss = map(int, m.groups())
            try:
                return datetime(y, mo, d, hh, mm, ss)
            except ValueError:
                return None
    return None


# время до даты: «19:56», «19.56», «19 56» и совсем без разделителя «1956»
_TIME_SEP = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*[:.,;]+\s*([0-5]\d)(?!\d)")
_TIME_BARE = re.compile(r"(?<!\d)([01]\d|2[0-3])([0-5]\d)(?!\d)")


def parse_clock(rows: list[str]) -> tuple[datetime | None, str]:
    """Из строк зоны часов вытащить дату и время.

    Игра печатает «19:56 02.09.2026» одной строкой. Дату надо вырезать до поиска времени:
    в «02.09.2026» само по себе прячется «02.09», и если OCR потерял разделитель в часах
    («1956» вместо «19:56»), временем ошибочно становится день с месяцем — а это меняет
    смену с дневной на ночную. Строку с температурой («+18.4° Облачно») тоже не смотрим.
    """
    date_row = next((r for r in rows if DATE_RE.search(r)), None)
    if date_row is None:
        return None, "нет даты"
    d = DATE_RE.search(date_row)
    before = date_row[: d.start()]          # всё, что левее даты, — там и стоят часы

    def grab(text: str):
        m = _TIME_SEP.search(text) or _TIME_BARE.search(text)
        return (int(m.group(1)), int(m.group(2))) if m else None

    hm = grab(before)
    if hm is None:
        for r in rows:
            if r is date_row or r.lstrip().startswith(("+", "-", "−")):
                continue
            hm = grab(r)
            if hm:
                break
    if hm is None:
        return None, "нет времени"
    try:
        return datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)), hm[0], hm[1]), ""
    except ValueError:
        return None, "дата/время не складываются"


@dataclass
class Facts:
    path: str
    hash: str = ""
    frame_w: int = 0
    frame_h: int = 0
    viewport_box: list[int] | None = None
    needs_crop: bool = False

    toasts: list[str] = field(default_factory=list)
    action: str | None = None
    action_conf: float = 0.0
    patient_id: str | None = None
    supporting: list[str] = field(default_factory=list)

    clock_rows: list[str] = field(default_factory=list)
    game_time: str | None = None
    file_time: str | None = None
    time: str | None = None
    time_source: str = ""

    safe_zone: bool | None = None
    zone_badge: str | None = None      # safe / danger / None
    minimap_text: list[str] = field(default_factory=list)
    map_place: str | None = None          # gkb1/gkb2 по подписи карты
    map_place_hits: list[str] = field(default_factory=list)

    chat_rows: list[str] = field(default_factory=list)
    chat_events: list[dict] = field(default_factory=list)
    call_panel: dict | None = None
    own_id: str | None = None

    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _rows(im, zone, key, upscale=1.5, min_conf=0.2):
    return ocr.join_rows(ocr.read_zone(im, zone, key=key, upscale=upscale, min_conf=min_conf))


def extract_cached(path: Path, *, want_chat: bool = False) -> dict:
    """Факты из кэша, если файл уже разбирали. Нужен, чтобы разметчик открывался сразу."""
    from .scan import SCAN_DIR
    h = ocr.file_hash(path)
    j = SCAN_DIR / f"{h}.json"
    if j.exists():
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
            if d.get("path") == str(path):
                return d
        except (OSError, ValueError):
            pass
    return extract(path, want_chat=want_chat).to_dict()


def extract(path: Path, *, want_chat: bool = True) -> Facts:
    f = Facts(path=str(path))
    f.hash = ocr.file_hash(path)
    im = Image.open(path)
    im.load()
    f.frame_w, f.frame_h = im.size
    ft = filename_time(path)
    f.file_time = ft.isoformat(timespec="minutes") if ft else None

    vp = viewport.detect(im)
    if vp is None:
        f.problems.append("не найдено окно игры (нет логотипа HUD)")
        # часы прочитать не из чего, но время у кадра всё равно есть: оно записано
        # в имени файла, а если нет — во времени создания. Без него файл получал бы
        # имя «без-времени» и терялся в общей куче.
        if ft:
            f.time, f.time_source = f.file_time, "имя файла"
        else:
            mt = datetime.fromtimestamp(path.stat().st_mtime)
            f.time, f.time_source = mt.isoformat(timespec="minutes"), "время файла"
        return f
    f.viewport_box = [vp.box.x0, vp.box.y0, vp.box.x1, vp.box.y1]
    f.needs_crop = not vp.is_full_frame
    h = f.hash

    # --- тосты: действие + ID -------------------------------------------------
    f.toasts = _rows(im, vp.zone_toasts, f"{h}:toasts", upscale=1.5)
    toast_pats = {k: v for k, v in _phr["toasts"].items() if v}
    found: list[tuple[str, float, str]] = []          # (действие, точность, строка)
    for row in f.toasts:
        tag, score = best_match(row, toast_pats)
        if score >= 0.72:
            found.append((tag, round(score, 3), row))
        ctag = match_contains(row, _phr.get("toasts_contains", {}))
        if ctag:
            found.append((ctag, 1.0, row))
        stag, sscore = best_match(row, _phr["supporting"])
        if sscore >= 0.72:
            f.supporting.append(stag)
    if found:
        # в кадре может висеть сразу несколько плашек: реанимация важнее отменённого вызова,
        # иначе один и тот же кадр засчитался бы как более дешёвое действие
        order = _phr.get("priority", [])
        rank = {a: i for i, a in enumerate(order)}
        tag, conf, row = min(found, key=lambda t: (rank.get(t[0], len(order)), -t[1]))
        f.action, f.action_conf = tag, conf
        f.patient_id = find_patient(row)
    if f.action and f.patient_id is None and f.action != "call_cancelled":
        f.problems.append("действие есть, ID пациента не прочитан")

    # --- часы ---------------------------------------------------------------
    f.clock_rows = _rows(im, vp.zone_clock, f"{h}:clock", upscale=2.0, min_conf=0.1)
    gt, why = parse_clock(f.clock_rows)
    f.game_time = gt.isoformat(timespec="minutes") if gt else None
    if ft:
        f.time, f.time_source = f.file_time, "имя файла"
        if gt and abs((gt - ft).total_seconds()) > 300:
            f.problems.append(f"часы в игре ({gt:%H:%M}) не сходятся с именем файла ({ft:%H:%M})")
    elif gt:
        f.time, f.time_source = f.game_time, "часы в игре"
    else:
        mt = datetime.fromtimestamp(path.stat().st_mtime)
        f.time, f.time_source = mt.isoformat(timespec="minutes"), "время файла (ненадёжно)"
        f.problems.append(f"часы не прочитаны ({why.strip()}), взято время файла")

    # --- зона: безопасная / опасная ----------------------------------------------
    sz = _rows(im, vp.zone_safezone, f"{h}:safe", upscale=2.0, min_conf=0.1)
    f.zone_badge = zone_badge(sz)
    f.safe_zone = f.zone_badge == "safe"

    # --- миникарта: подписи ------------------------------------------------------
    f.minimap_text = [r for r in _rows(im, vp.zone_minimap, f"{h}:map", upscale=2.0, min_conf=0.15)
                      if len(r) >= 2]
    f.map_place, f.map_place_hits = match_map_place(f.minimap_text)

    # --- правый верх: свой ID ------------------------------------------------------
    tr = " ".join(_rows(im, vp.zone_topright, f"{h}:tr", upscale=2.0, min_conf=0.1))
    m = re.search(r"ID[:\s]*(\d{2,5})", tr)
    if m:
        f.own_id = m.group(1)

    # --- чат и панель вызова (контекст) --------------------------------------------
    if want_chat:
        f.chat_rows = merge_wrapped(_rows(im, vp.zone_chat, f"{h}:chat", upscale=1.0, min_conf=0.2))
        for row in f.chat_rows:
            tag, score = best_match(row, _phr["chat"])
            if score >= 0.6:
                f.chat_events.append({"tag": tag, "id": find_patient(row),
                                      "score": round(score, 2)})
        cp = _rows(im, vp.zone_call_panel, f"{h}:call", upscale=1.5, min_conf=0.2)
        if any(similarity(r, _phr["call_panel"]["header"]) >= 0.7 for r in cp):
            joined = " ".join(cp)
            cid = find_patient_id(joined)
            reason = re.search(r"Причина[:\s]*([А-Яа-яA-Za-z ]+)", joined)
            f.call_panel = {"id": cid,
                            "reason": reason.group(1).strip() if reason else None, "rows": cp}

    if f.action is None:
        f.problems.append("нет плашки действия")
    return f


def match_map_place(rows: list[str]) -> tuple[str | None, list[str]]:
    """По подписям миникарты — больница. Прямая подпись «N БОЛЬНИЦА» весит больше ориентиров."""
    hits: dict[str, list[str]] = {}
    text = " ".join(rows).upper().replace("Ё", "Е")
    for hid, info in _places["hospitals"].items():
        lab = info["map_label"].upper()
        # OCR путает Ь/Е/Ы: «БОЛЬНИЦА» может прийти как «БОЛЕНИЦА» или «БОЛЬНИЦА»
        if re.search(re.escape(lab[0]) + r"\s*БОЛ[ЬЕЫ]?Н", text):
            hits.setdefault(hid, []).append(lab)
        for lm in info["landmarks"]:
            lmu = lm.upper().replace("Ё", "Е")
            if len(lmu) >= 4:
                if lmu in text:
                    hits.setdefault(hid, []).append(lm)
            elif re.search(r"(?<![\wА-Я])" + re.escape(lmu) + r"(?![\wА-Я])", text):
                hits.setdefault(hid, []).append(lm)
    if not hits:
        return None, []
    for hid, hs in hits.items():
        if any("БОЛЬНИЦА" in x for x in hs):
            return hid, hs
    best = max(hits, key=lambda k: len(hits[k]))
    tie = [k for k in hits if k != best and len(hits[k]) >= len(hits[best])]
    if tie:
        return None, sum(hits.values(), [])
    return best, hits[best]


def merge_wrapped(rows: list[str]) -> list[str]:
    """Чат переносит слова дефисом («...и пе-» / «редал #57405») — склеиваем."""
    out: list[str] = []
    for r in rows:
        if out and out[-1].rstrip().endswith("-"):
            out[-1] = out[-1].rstrip()[:-1] + r.lstrip()
        else:
            out.append(r)
    return out


def zone_badge(rows: list[str]) -> str | None:
    """«БЕЗОПАСНО» или «ОПАСНО» внизу слева.

    Слова различаются приставкой, но по буквам похожи на 80% — нечёткое сравнение их путает,
    поэтому приставку проверяем отдельно.
    """
    for r in rows:
        t = re.sub(r"[^А-ЯA-Z]", "", r.upper().replace("Ё", "Е"))
        if len(t) < 6:
            continue
        if t.startswith("БЕЗ") and difflib.SequenceMatcher(None, t, "БЕЗОПАСНО").ratio() >= 0.7:
            return "safe"
        if difflib.SequenceMatcher(None, t, "ОПАСНО").ratio() >= 0.8:
            return "danger"
    return None
