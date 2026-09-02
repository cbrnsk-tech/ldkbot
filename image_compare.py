"""
Сравнение двух фотографий одного и того же пункта осмотра (текущее фото
против фото того же пункта из предыдущего осмотра той же машины).

Используется структурное сходство (SSIM) из scikit-image — оно устойчивее
к небольшим изменениям освещения/ракурса, чем простое попиксельное
сравнение, и хорошо подходит для задачи "нашлось что-то новое или нет".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as ssim

logger = logging.getLogger(__name__)

COMPARE_SIZE = (400, 400)  # приводим оба фото к одному размеру перед сравнением


def _load_gray(path: Path) -> np.ndarray:
    img = Image.open(path).convert("L")  # градации серого
    img = img.resize(COMPARE_SIZE)
    return np.array(img)


def compare_images(current_path: Path, previous_path: Optional[Path]) -> dict:
    """
    Возвращает {"similarity": float 0..1, "is_damaged": bool, "error": str|None}.
    similarity = 1.0 означает "фото идентичны".
    Если previous_path нет (это первый осмотр машины) — считаем, что
    сравнивать не с чем, is_damaged=False.
    """
    if previous_path is None or not Path(previous_path).exists():
        return {"similarity": None, "is_damaged": False, "error": None}

    try:
        img_a = _load_gray(Path(current_path))
        img_b = _load_gray(Path(previous_path))
        score = float(ssim(img_a, img_b))
        import config as _config  # локальный импорт, чтобы избежать циклов

        is_damaged = score < _config.IMAGE_SIMILARITY_THRESHOLD
        return {"similarity": round(score, 3), "is_damaged": is_damaged, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при сравнении фото")
        return {"similarity": None, "is_damaged": False, "error": str(exc)}
