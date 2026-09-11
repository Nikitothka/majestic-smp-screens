"""Разборщик скриншотов больничной работы Majestic RP (Россия Онлайн)."""
__version__ = "0.1.0"

# DINOv2 предупреждает про отсутствие xFormers при каждой загрузке. Ускоритель
# не обязателен, а в логе это выглядит пугающе — убираем из вывода.
import warnings as _warnings
_warnings.filterwarnings("ignore", message=".*xFormers.*")
