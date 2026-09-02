"""
Обёртка над Google Sheets (через gspread).

Требуется:
  1. Сервисный аккаунт Google (JSON-ключ) — путь указывается в
     config.GOOGLE_CREDS_PATH
  2. Таблица должна быть "расшарена" (Share) на email сервисного аккаунта
     с правами Editor
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from functools import lru_cache
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _normalize(text: str) -> str:
    """Убирает лишние пробелы и приводит к нижнему регистру — чтобы
    различия вроде "Премия день  С НДС" и "премия день с ндс" считались
    одним и тем же столбцом."""
    return re.sub(r"\s+", " ", str(text).strip()).lower()


@lru_cache(maxsize=1)
def _get_client() -> gspread.Client:
    if config.GOOGLE_CREDS_JSON:
        # ключ передан целиком через переменную окружения (Railway/Render)
        raw = config.GOOGLE_CREDS_JSON.strip()
        if not raw.startswith("{"):
            # частая ошибка: скопировали не весь файл, без внешних { }
            raw = "{" + raw + "}"
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_CREDS_JSON повреждён — похоже, скопирован не весь "
                "JSON-файл ключа целиком (от первой { до последней }). "
                f"Исходная ошибка: {exc}"
            ) from exc
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # ключ лежит файлом рядом с ботом (VPS)
        creds = Credentials.from_service_account_file(config.GOOGLE_CREDS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_worksheet(sheet_id: str, worksheet_name: str):
    client = _get_client()
    sh = client.open_by_key(sheet_id)
    return sh.worksheet(worksheet_name)


class DriverNotFound(Exception):
    pass


RU_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4,
    "май": 5, "июнь": 6, "июль": 7, "август": 8,
    "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def _parse_period(text: str) -> tuple[int, int]:
    """Пытается разобрать период вида 'июль 26' / 'Август 26' в (год, месяц)
    для сортировки. Если не получилось — возвращает (-1, -1), такие строки
    уйдут в конец списка (как самые старые/неизвестные)."""
    parts = _normalize(text).split()
    if len(parts) < 2:
        return (-1, -1)
    month = RU_MONTHS.get(parts[0])
    if month is None:
        return (-1, -1)
    try:
        year = int(parts[1])
    except ValueError:
        return (-1, -1)
    if year < 100:
        year += 2000
    return (year, month)


def get_salary_history(fio: str) -> list[dict]:
    """
    Возвращает список записей по ФИО — по одной на каждый месяц, где он
    встретился в таблице, отсортированный от новых к старым:
    [{"period": "Август 26", "oklad": ..., "premiya_den": ...,
      "itogo_premiya": ...}, ...]

    Месяц/период берётся из столбца, идущего сразу ПОСЛЕ столбца ФИО —
    у него в этой таблице нет текста в заголовке, поэтому искать по имени
    столбца нельзя, используется его позиция.
    """
    if not config.SALARY_SHEET_ID:
        raise RuntimeError("SALARY_SHEET_ID не задан в переменных окружения")

    ws = _open_worksheet(config.SALARY_SHEET_ID, config.SALARY_WORKSHEET_NAME)
    all_values = ws.get_all_values()  # список списков: [ [заголовки], [строка1], ... ]
    if not all_values:
        raise DriverNotFound("Таблица пуста")

    headers = all_values[0]
    # для каждого нормализованного названия столбца запоминаем ПЕРВЫЙ индекс,
    # где оно встретилось — так дубликаты/пустые заголовки не ломают поиск
    header_index: dict[str, int] = {}
    for idx, h in enumerate(headers):
        key = _normalize(h)
        if key and key not in header_index:
            header_index[key] = idx

    def col(name: str) -> Optional[int]:
        return header_index.get(_normalize(name))

    idx_fio = col(config.COL_FIO)
    if idx_fio is None:
        raise RuntimeError(
            f"В листе '{config.SALARY_WORKSHEET_NAME}' не найден столбец "
            f"'{config.COL_FIO}'. Проверьте точное название заголовка в таблице."
        )
    idx_month = idx_fio + 1  # столбец периода — сразу справа от ФИО, без заголовка
    idx_oklad = col(config.COL_OKLAD)
    idx_premiya_den = col(config.COL_PREMIYA_DEN_NDS)
    idx_itogo = col(config.COL_ITOGO_PREMIYA_NDS)

    fio_norm = _normalize(fio)

    def get_cell(row: list, idx: Optional[int]) -> str:
        if idx is None or idx >= len(row):
            return "—"
        return row[idx] or "—"

    results = []
    for row in all_values[1:]:
        if idx_fio < len(row) and _normalize(row[idx_fio]) == fio_norm:
            period = get_cell(row, idx_month)
            results.append(
                {
                    "period": period if period != "—" else "(без периода)",
                    "oklad": get_cell(row, idx_oklad),
                    "premiya_den": get_cell(row, idx_premiya_den),
                    "itogo_premiya": get_cell(row, idx_itogo),
                    "_sort_key": _parse_period(period),
                }
            )

    if not results:
        raise DriverNotFound(f"Водитель с ФИО '{fio}' не найден в таблице")

    results.sort(key=lambda r: r["_sort_key"], reverse=True)
    for r in results:
        del r["_sort_key"]
    return results


def log_inspection(driver_fio: str, vehicle_number: str, summary: dict) -> None:
    """
    Дописывает строку с результатом осмотра в лист-лог осмотров.
    summary — словарь, который формирует bot.py в _finish_inspection
    (odometer_km, driver_reported_damages, auto_flagged, equipment, comment).
    Если INSPECTIONS_SHEET_ID не задан — ничего не делает (лог остаётся
    только в локальных json-файлах).
    """
    if not config.INSPECTIONS_SHEET_ID:
        return

    try:
        ws = _open_worksheet(config.INSPECTIONS_SHEET_ID, config.INSPECTIONS_WORKSHEET_NAME)

        damages = summary.get("driver_reported_damages") or []
        damages_str = "; ".join(d["description"] for d in damages) if damages else "нет"

        auto_flagged = summary.get("auto_flagged") or []
        auto_flagged_str = ", ".join(auto_flagged) if auto_flagged else "нет"

        equipment = summary.get("equipment") or {}
        missing = [title for title, present in equipment.items() if not present]
        missing_str = ", ".join(missing) if missing else "всё на месте"

        ws.append_row(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                driver_fio,
                vehicle_number,
                summary.get("odometer_km", ""),
                summary.get("fuel_level", ""),
                damages_str,
                auto_flagged_str,
                missing_str,
                summary.get("comment", ""),
            ],
            value_input_option="USER_ENTERED",
        )
    except Exception:
        logger.exception("Не удалось записать лог осмотра в Google Sheets")
