# -*- coding: utf-8 -*-
"""Пути, источники скриншотов и загрузка правил.

Здесь разведены две вещи. Правила фракции (баллы, нормативы, замены) — общие для всех
медиков и лежат в rules.yaml внутри программы. Личный выбор каждого (куда идёт на
повышение, сколько вакцин держать про запас, папки, рубежи сдачи) — в data/settings.json
и поверх правил. Так обновление программы не затирает чужие настройки.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent
DATA_DIR = PROJECT_DIR / "data"          # личное: кэш, журнал настроек, свои эталоны
ASSETS_DIR = PKG_DIR / "assets"          # общее: эталоны больниц и модели, едут с программой
PLACE_REFS_DIR = DATA_DIR / "place_refs"
BASE_REFS_DIR = ASSETS_DIR / "place_refs"
CACHE_DB = DATA_DIR / "cache.sqlite"
SETTINGS_FILE = DATA_DIR / "settings.json"

# Папки, откуда берутся скриншоты. Добавить новую — дописать строку
# (или кнопкой «Добавить папку» в окне программы).
DEFAULT_SOURCES = [
    Path(os.path.expanduser(r"~\Videos\NVIDIA\Grand Theft Auto V")),
]

def _personal_dest() -> Path:
    """Своя папка сотрудника из настроек, иначе «Документы\Отчёты СМП»."""
    try:
        d = json.loads((PROJECT_DIR / "data" / "settings.json").read_text(encoding="utf-8")).get("dest")
    except (OSError, ValueError):
        d = None
    return Path(d) if d else Path.home() / "Documents" / "Отчёты СМП"


# Куда раскладывать. Меняется в окне программы и запоминается в личных настройках.
DEFAULT_DEST = _personal_dest()

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}

# Служебные папки в каталоге назначения — их содержимое не является исходником.
SERVICE_DIRS = {
    "manual": "❓ Проверить вручную",
    "no_action": "_без действия",
    "swaps": "Замены",
    "duplicates": "_дубликаты",
    "originals": "_оригиналы (можно удалить)",
    "report": "_отчёт",
}


@dataclass
class Rules:
    """Балловая система, прочитанная из rules.yaml."""

    raw: dict = field(default_factory=dict)

    @property
    def times(self) -> dict:
        return self.raw["times"]

    @property
    def actions(self) -> dict:
        return self.raw["actions"]

    @property
    def categories(self) -> list[dict]:
        return self.raw["categories"]

    @property
    def lunch_applies_to(self):
        return self.raw.get("lunch_applies_to", "all")

    @property
    def substitutions(self) -> list[dict]:
        return self.raw.get("substitutions", [])

    def place_precision(self, action: str) -> str:
        """Насколько точно нужно знать место для этого действия."""
        return self.actions.get(action, {}).get("place", "none")


def load_settings() -> dict:
    """Личные настройки сотрудника. Пустой словарь, если их ещё нет."""
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(update: dict) -> dict:
    s = load_settings()
    s.update(update)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    return s


def load_rules(path: Path | None = None, *, personal: bool = True) -> Rules:
    """Правила фракции, поверх которых наложен личный выбор сотрудника."""
    path = path or PKG_DIR / "rules.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if personal:
        s = load_settings()
        pr = raw.setdefault("promotions", {})
        if "target_rank" in s:
            pr["target"] = s["target_rank"] or ""
        if "spare_vaccines" in s:
            pr.setdefault("spare", {})["vaccines"] = int(s["spare_vaccines"])
    return Rules(raw)


def load_phrases(path: Path | None = None) -> dict:
    path = path or PKG_DIR / "phrases.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))
