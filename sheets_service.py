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
        info = json.loads(config.GOOGLE_CREDS_JSON)
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


def get_salary_info(fio: str) -> dict:
    """
    Ищет строку по ФИО в таблице зарплат и возвращает словарь
    {"oklad": ..., "premiya_den": ..., "itogo_premiya": ...}.
    Сравнение ФИО и названий столбцов регистронезависимое, лишние пробелы
    в заголовках игнорируются.
    """
    if not config.SALARY_SHEET_ID:
        raise RuntimeError("SALARY_SHEET_ID не задан в переменных окружения")

    ws = _open_worksheet(config.SALARY_SHEET_ID, config.SALARY_WORKSHEET_NAME)
    records = ws.get_all_records()  # список dict по заголовкам первой строки

    fio_norm = _normalize(fio)
    col_fio = _normalize(config.COL_FIO)
    col_oklad = _normalize(config.COL_OKLAD)
    col_premiya_den = _normalize(config.COL_PREMIYA_DEN_NDS)
    col_itogo = _normalize(config.COL_ITOGO_PREMIYA_NDS)

    for row in records:
        row_norm = {_normalize(k): v for k, v in row.items()}
        if _normalize(row_norm.get(col_fio, "")) == fio_norm:
            return {
                "oklad": row_norm.get(col_oklad, "—"),
                "premiya_den": row_norm.get(col_premiya_den, "—"),
                "itogo_premiya": row_norm.get(col_itogo, "—"),
            }

    raise DriverNotFound(f"Водитель с ФИО '{fio}' не найден в таблице")


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
                damages_str,
                auto_flagged_str,
                missing_str,
                summary.get("comment", ""),
            ],
            value_input_option="USER_ENTERED",
        )
    except Exception:
        logger.exception("Не удалось записать лог осмотра в Google Sheets")
