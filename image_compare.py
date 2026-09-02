"""
Сравнение двух фотографий одного и того же пункта осмотра (текущее фото
против фото того же пункта из предыдущего осмотра той же машины).

Специально не используется scikit-image (SSIM) — эта библиотека на многих
хостингах (например Railway) пытается собираться из исходников и падает
на сборке. Вместо этого используется простое сравнение через Pillow и
numpy: приводим оба фото к одному размеру, переводим в градации серого и
считаем среднюю разницу яркости пикселей. Для задачи "заметно отличается
фото или нет" этого достаточно.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

COMPARE_SIZE = (200, 200)  # приводим оба фото к одному размеру перед сравнением


def _load_gray(path: Path) -> np.ndarray:
    img = Image.open(path).convert("L")  # градации серого
    img = img.resize(COMPARE_SIZE)
    return np.asarray(img, dtype=np.float32)


def compare_images(current_path: Path, previous_path: Optional[Path]) -> dict:
    """
    Возвращает {"similarity": float 0..1, "is_damaged": bool, "error": str|None}.
    similarity = 1.0 означает "фото практически идентичны".
    Если previous_path нет (это первый осмотр машины) — считаем, что
    сравнивать не с чем, is_damaged=False.
    """
    if previous_path is None or not Path(previous_path).exists():
        return {"similarity": None, "is_damaged": False, "error": None}

    try:
        img_a = _load_gray(Path(current_path))
        img_b = _load_gray(Path(previous_path))

        # средняя абсолютная разница яркости пикселей, нормированная на 0..1
        mean_abs_diff = float(np.mean(np.abs(img_a - img_b)))
        similarity = max(0.0, 1.0 - mean_abs_diff / 255.0)

        import config as _config  # локальный импорт, чтобы избежать циклов

        is_damaged = similarity < _config.IMAGE_SIMILARITY_THRESHOLD
        return {"similarity": round(similarity, 3), "is_damaged": is_damaged, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при сравнении фото")
        return {"similarity": None, "is_damaged": False, "error": str(exc)}
