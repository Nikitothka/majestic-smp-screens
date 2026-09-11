# -*- coding: utf-8 -*-
"""Определение места по интерьеру: эмбеддинг сцены → kNN по размеченным эталонам.

Эталоны лежат в data/place_refs/<метка>/ (gkb1, gkb2, asmp, street). Это либо целые
скриншоты (окно игры найдётся само), либо уже вырезанные сцены. Эмбеддинги кэшируются
по хэшу файла, так что повторная сборка индекса мгновенна.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from . import ocr, viewport
from .config import PLACE_REFS_DIR, BASE_REFS_DIR, ASSETS_DIR, DATA_DIR, IMAGE_SUFFIXES

LABELS = ["gkb1", "gkb2", "asmp", "street"]
HOSPITAL_LABELS = {"gkb1", "gkb2"}
SCENE_SIZE = (640, 384)     # размер, в котором храним вырезанные сцены-эталоны
INDEX_FILE = DATA_DIR / "place_index.npz"
INDOOR_FILE = DATA_DIR / "indoor_model.npz"
OVERRIDES_FILE = DATA_DIR / "place_overrides.json"
WALL_FILE = DATA_DIR / "wall_color_model.json"

_model = None
import os


def _pick_device() -> str:
    if torch.cuda.is_available() and os.environ.get("MEDREPORT_DEVICE", "").lower() != "cpu":
        return "cuda"
    return "cpu"


_device = _pick_device()
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _load_model():
    """DINOv2 ViT-S/14 — лучше всего различает помещения. Запасной вариант — ResNet50."""
    global _model, _device
    dev = _pick_device()
    if _model is not None and dev == _device:
        return _model
    _device = dev
    try:
        # DINOv2 при загрузке жалуется, что нет ускорителя xFormers. Он не нужен —
        # модель прекрасно работает и без него, а в логе это выглядит как три ошибки.
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*xFormers.*")
            m = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14", verbose=False)
        kind = "dinov2"
    except Exception:  # noqa: BLE001 — нет интернета / hub недоступен
        import torchvision
        m = torchvision.models.resnet50(weights=torchvision.models.ResNet50_Weights.IMAGENET1K_V2)
        m.fc = torch.nn.Identity()
        kind = "resnet50"
    m.eval().to(_device)
    _model = (m, kind)
    return _model


def scene_crop(im: Image.Image) -> Image.Image:
    """Вырезать игровую сцену без HUD. Если окно игры не найдено — считаем, что это уже сцена."""
    vp = viewport.detect(im)
    crop = vp.zone_scene.crop(im) if vp else im
    return crop.convert("RGB").resize(SCENE_SIZE, Image.LANCZOS)


@torch.no_grad()
def embed_image(scene: Image.Image) -> np.ndarray:
    model, kind = _load_model()
    size = 224 if kind == "dinov2" else 224
    x = np.asarray(scene.resize((size, size), Image.BICUBIC), dtype=np.float32) / 255.0
    x = (x - _MEAN) / _STD
    t = torch.from_numpy(x).permute(2, 0, 1).unsqueeze(0).to(_device)
    v = model(t)
    v = v.flatten().float().cpu().numpy()
    return v / (np.linalg.norm(v) + 1e-8)


def embed_file(path: Path, file_hash: str | None = None) -> np.ndarray:
    h = file_hash or ocr.file_hash(path)
    key = f"{h}:emb"
    hit = ocr.cache().get(key)
    if hit is not None:
        return np.asarray(hit, dtype=np.float32)
    im = Image.open(path)
    im.load()
    v = embed_image(scene_crop(im))
    ocr.cache().put(key, [float(x) for x in v])
    return v


@dataclass
class PlaceGuess:
    label: str | None
    confidence: float
    top_sim: float
    votes: dict

    @property
    def in_hospital(self) -> bool | None:
        if self.label is None:
            return None
        return self.label in HOSPITAL_LABELS


class PlaceIndex:
    def __init__(self):
        self.vecs = np.zeros((0, 384), dtype=np.float32)
        self.labels: list[str] = []
        self.files: list[str] = []

    @classmethod
    def build(cls, refs_dir: Path | None = None, *, log=print) -> "PlaceIndex":
        """Эталоны берутся из двух мест: общие едут с программой (их собрали на реальных
        скринах, и игра у всех одна), свои сотрудник добавляет разметкой."""
        idx = cls()
        vecs, labels, files = [], [], []
        dirs = [refs_dir] if refs_dir else [BASE_REFS_DIR, PLACE_REFS_DIR]
        seen: set[str] = set()
        for root in dirs:
            for d in sorted(root.iterdir()) if root.exists() else []:
                if not d.is_dir() or d.name.startswith("_"):
                    continue
                label = d.name
                for p in sorted(d.iterdir()):
                    if p.suffix.lower() not in IMAGE_SUFFIXES or p.name in seen:
                        continue
                    seen.add(p.name)              # один и тот же эталон дважды не считаем
                    vecs.append(embed_file(p))
                    labels.append(label)
                    files.append(str(p))
        if vecs:
            idx.vecs = np.stack(vecs)
        idx.labels, idx.files = labels, files
        log(f"индекс мест: {len(labels)} эталонов, метки: {sorted(set(labels))}")
        return idx

    def save(self, path: Path = INDEX_FILE) -> None:
        np.savez(path, vecs=self.vecs, labels=np.array(self.labels), files=np.array(self.files))

    @classmethod
    def load(cls, path: Path = INDEX_FILE) -> "PlaceIndex | None":
        if not path.exists():
            return None
        z = np.load(path, allow_pickle=False)
        idx = cls()
        idx.vecs, idx.labels, idx.files = z["vecs"], list(z["labels"]), list(z["files"])
        return idx

    def __len__(self) -> int:
        return len(self.labels)

    def nearest_sim(self, v: np.ndarray, labels) -> float:
        """Похожесть на ближайший эталон из указанных меток (0, если таких эталонов нет)."""
        if len(self) == 0:
            return 0.0
        mask = np.array([l in labels for l in self.labels])
        if not mask.any():
            return 0.0
        return float((self.vecs[mask] @ v).max())

    def classify(self, v: np.ndarray, k: int = 5, min_conf: float = 0.6) -> PlaceGuess:
        # Пока размечена одна метка, kNN может ответить только ею — это не знание, а эхо.
        # Требуем хотя бы две метки и по три эталона у победившей.
        if len(self) == 0 or len(set(self.labels)) < 2:
            return PlaceGuess(None, 0.0, 0.0, {})
        sims = self.vecs @ v
        order = np.argsort(-sims)[:k]
        votes: dict[str, float] = {}
        for i in order:
            votes[self.labels[i]] = votes.get(self.labels[i], 0.0) + float(sims[i])
        label = max(votes, key=votes.get)
        share = votes[label] / max(1e-8, sum(votes.values()))
        if self.labels.count(label) < 3:
            return PlaceGuess(None, 0.0, float(sims[order[0]]), votes)
        top_sim = float(sims[order[0]])
        # уверенность: доля голосов × близость лучшего соседа
        conf = share * max(0.0, min(1.0, (top_sim - 0.3) / 0.6))
        if conf < min_conf:
            return PlaceGuess(None, round(conf, 3), round(top_sim, 3), votes)
        return PlaceGuess(label, round(conf, 3), round(top_sim, 3), votes)


# ---------------------------------------------------------------- разметка
def cluster(vecs: np.ndarray, k: int) -> np.ndarray:
    """k-means по косинусу; возвращает номер кластера для каждой строки."""
    from scipy.cluster.vq import kmeans2
    k = max(1, min(k, len(vecs)))
    rng = np.random.default_rng(0)
    _, lab = kmeans2(vecs.astype(np.float64), k, minit="++", seed=rng, iter=50)
    return lab


def save_reference(path: Path, label: str, file_hash: str | None = None) -> Path:
    """Сохранить сцену со скриншота как эталон метки (компактный кроп 640×384)."""
    d = PLACE_REFS_DIR / label
    d.mkdir(parents=True, exist_ok=True)
    h = file_hash or ocr.file_hash(path)
    out = d / f"{h}.jpg"
    if not out.exists():
        im = Image.open(path)
        im.load()
        scene_crop(im).save(out, quality=88)
    return out


# ------------------------------------------------- внутри помещения или на улице
class IndoorModel:
    """Различает «в помещении» и «на улице» — этого хватает, чтобы решить,
    ПМП это в больнице (1 балл) или на выезде (3–5 баллов).

    Учится на самом корпусе без разметки: лечение и вакцинация физически невозможны
    на улице, поэтому их кадры — эталон «внутри», а реанимации в основном уличные.
    Два центроида, решение по разнице косинусов.
    """

    INSIDE_ACTIONS = {"heal", "vaccinate", "certificate"}
    MIN_EXAMPLES = 12

    def __init__(self, inside=None, street=None, margin: float = 0.02):
        self.inside = inside
        self.street = street
        self.margin = margin

    @property
    def ready(self) -> bool:
        return self.inside is not None and self.street is not None

    @classmethod
    def fit(cls, vecs: np.ndarray, actions: list[str], *, log=print) -> "IndoorModel":
        a = np.asarray(actions)
        ins = np.isin(a, list(cls.INSIDE_ACTIONS))
        out = a == "revive"
        if ins.sum() < cls.MIN_EXAMPLES or out.sum() < cls.MIN_EXAMPLES:
            log(f"внутри/улица: мало данных ({ins.sum()} и {out.sum()}), модель не строится")
            return cls()
        ci, cs = vecs[ins].mean(0), vecs[out].mean(0)
        ci /= np.linalg.norm(ci)
        cs /= np.linalg.norm(cs)
        m = cls(ci, cs)
        agree = np.mean((vecs[ins] @ ci - vecs[ins] @ cs) > 0)
        log(f"внутри/улица: обучено на {ins.sum()}+{out.sum()} кадрах, "
            f"согласие с правилом «лечение только в помещении» {100 * agree:.0f}%")
        return m

    def predict(self, v: np.ndarray) -> tuple[str | None, float]:
        # общая модель посчитана на DINOv2; если у сотрудника она не скачалась и включилась
        # запасная ResNet, размерность другая — тогда молчим, а не падаем
        if not self.ready or self.inside.shape != v.shape:
            return None, 0.0
        d = float(v @ self.inside - v @ self.street)
        if abs(d) < self.margin:
            return None, abs(d)
        return ("inside" if d > 0 else "street"), abs(d)

    def save(self, path: Path = INDOOR_FILE) -> None:
        if self.ready:
            np.savez(path, inside=self.inside, street=self.street, margin=self.margin)

    @classmethod
    def load(cls, path: Path = INDOOR_FILE) -> "IndoorModel":
        """Своя модель, если уже обучилась на своих скринах, иначе общая из программы."""
        for p in (path, ASSETS_DIR / "indoor_model.npz"):
            if p.exists():
                z = np.load(p)
                return cls(z["inside"], z["street"], float(z["margin"]))
        return cls()


# ------------------------------------------------------- ручные метки по хэшу
def load_overrides(path: Path = OVERRIDES_FILE) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_overrides(data: dict, path: Path = OVERRIDES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def set_override(file_hash: str, label: str, source: str, path: Path = OVERRIDES_FILE) -> None:
    d = load_overrides(path)
    d[file_hash] = {"place": label, "source": source}
    save_overrides(d, path)


# --------------------------------------------- больница по цвету стен и освещению
def wall_feature(path: Path, viewport_box: list[int] | None, frame: tuple[int, int],
                 file_hash: str | None = None) -> float | None:
    """Насколько стены тёплые: медиана (красный − синий) по верху кадра.

    У двух больниц в игре заметно разный ремонт и свет: одна бежевая и тёплая,
    другая серо-голубая и холодная. Разница видна даже когда сам ракурс новый,
    поэтому это надёжнее, чем сравнивать интерьеры целиком.

    Берётся только верхняя часть сцены (там стены, а не пол и люди) и только
    малонасыщенные пиксели — красная форма скорой и кожа выбрасываются.
    """
    if not viewport_box:
        return None
    h = file_hash or ocr.file_hash(path)
    key = f"{h}:wall"
    hit = ocr.cache().get(key)
    if hit is not None:
        return hit if hit is None else float(hit)
    from .viewport import Box, Viewport
    x0, y0, x1, y1 = viewport_box
    vp = Viewport(box=Box(x0, y0, x1, y1), scale=1.0, logo=Box(0, 0, 1, 1),
                  frame_w=frame[0], frame_h=frame[1])
    im = Image.open(path)
    im.load()
    a = np.asarray(vp.zone_scene.crop(im).convert("RGB").resize((320, 192), Image.BILINEAR),
                   dtype=np.float32) / 255.0
    top = a[: int(a.shape[0] * 0.55)].reshape(-1, 3)
    mx, mn = top.max(1), top.min(1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    keep = (sat < 0.35) & (mx > 0.12) & (mx < 0.98)
    w = top[keep] if keep.sum() > 300 else top
    v = float(np.median(w[:, 0] - w[:, 2]))
    ocr.cache().put(key, v)
    return v


class WallColorModel:
    """Различает больницы по теплоте стен — но только на кадрах выдачи таблеток.

    Осторожно: сам по себе цвет отделяет КОМНАТЫ, а не здания. В одной и той же больнице
    лаборатория холодная серо-голубая, а кабинет врача тёплый бежевый — на вакцинациях
    признак скачет и врёт. А вот таблетки выдают в регистратуре, и она у двух больниц
    отделана по-разному: у Склифа серо-голубая с красной вывеской «Распределительный пост»,
    у ГКБ 1 бежевая с расписанием участковых терапевтов. Поэтому модель учится и отвечает
    только по кадрам с таблетками, а на всю смену ответ распространяется отдельно.

    Граница находится по данным (два пика), а какая группа какая — по кадрам, где игра
    подписала больницу на карте, или по ручной разметке. Без такой привязки молчит.
    """

    #: действия, по кадрам которых можно судить о больнице (одна и та же комната в обеих)
    RELIABLE_ACTIONS = {"heal"}

    MIN_PER_SIDE = 5
    MIN_GAP = 0.020          # минимальный разрыв между центрами, иначе это одна группа

    def __init__(self, threshold: float | None = None, centres: tuple[float, float] | None = None,
                 mapping: dict | None = None, margin: float = 0.008):
        self.threshold = threshold
        self.centres = centres          # (холодный, тёплый)
        self.mapping = mapping or {}    # {"cold": "gkb2", "warm": "gkb1"}
        self.margin = margin

    @property
    def ready(self) -> bool:
        return self.threshold is not None and len(self.mapping) == 2

    @classmethod
    def fit(cls, values: dict[str, float], anchors: dict[str, str], *, log=print) -> "WallColorModel":
        vals = np.array([v for v in values.values() if v is not None], dtype=np.float64)
        if len(vals) < 2 * cls.MIN_PER_SIDE:
            log("цвет стен: мало кадров, модель не строится")
            return cls()
        # два кластера по одномерному признаку
        lo, hi = vals.min(), vals.max()
        c = np.array([lo + (hi - lo) * 0.25, lo + (hi - lo) * 0.75])
        for _ in range(50):
            side = np.abs(vals[:, None] - c[None, :]).argmin(1)
            if not (side == 0).any() or not (side == 1).any():
                break
            nc = np.array([vals[side == 0].mean(), vals[side == 1].mean()])
            if np.allclose(nc, c):
                break
            c = nc
        cold, warm = float(min(c)), float(max(c))
        if warm - cold < cls.MIN_GAP:
            log(f"цвет стен: группы не разделяются (разрыв {warm - cold:.3f}) — похоже, больница одна")
            return cls()
        thr = (cold + warm) / 2
        n_cold = int((vals < thr).sum())
        n_warm = len(vals) - n_cold
        if min(n_cold, n_warm) < cls.MIN_PER_SIDE:
            log("цвет стен: во второй группе слишком мало кадров, модель не строится")
            return cls()

        # привязка групп к названиям по подтверждённым кадрам
        votes: dict[str, dict[str, int]] = {"cold": {}, "warm": {}}
        for h, label in anchors.items():
            v = values.get(h)
            if v is None:
                continue
            side = "cold" if v < thr else "warm"
            votes[side][label] = votes[side].get(label, 0) + 1
        mapping = {}
        for side in ("cold", "warm"):
            if votes[side]:
                mapping[side] = max(votes[side], key=votes[side].get)
        # если известна одна сторона, вторая — «та, другая» из двух больниц
        known = set(mapping.values())
        if len(mapping) == 1 and known <= HOSPITAL_LABELS:
            other = (HOSPITAL_LABELS - known).pop()
            mapping["warm" if "cold" in mapping else "cold"] = other
        if len(mapping) != 2:
            log(f"цвет стен: две группы найдены ({n_cold} холодных / {n_warm} тёплых), "
                f"но неизвестно, какая больница какая — нужна разметка одной смены")
            return cls(thr, (cold, warm))
        m = cls(thr, (cold, warm), mapping)
        log(f"цвет стен: {n_cold} холодных → {mapping['cold']}, {n_warm} тёплых → {mapping['warm']} "
            f"(граница {thr:+.3f}, разрыв {warm - cold:.3f})")
        return m

    def predict(self, value: float | None) -> tuple[str | None, float]:
        if value is None or not self.ready:
            return None, 0.0
        d = value - self.threshold
        if abs(d) < self.margin:
            return None, abs(d)
        return self.mapping["warm" if d > 0 else "cold"], abs(d)

    def to_dict(self) -> dict:
        return {"threshold": self.threshold, "centres": self.centres,
                "mapping": self.mapping, "margin": self.margin}

    def save(self, path: Path = WALL_FILE) -> None:
        if self.threshold is not None:
            path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = WALL_FILE) -> "WallColorModel":
        src = next((p for p in (path, ASSETS_DIR / "wall_color_model.json") if p.exists()), None)
        if src is None:
            return cls()
        d = json.loads(src.read_text(encoding="utf-8"))
        return cls(d.get("threshold"), tuple(d["centres"]) if d.get("centres") else None,
                   d.get("mapping"), d.get("margin", 0.008))


def release() -> None:
    """Выгрузить модель распознавания сцены из видеопамяти."""
    global _model
    _model = None
