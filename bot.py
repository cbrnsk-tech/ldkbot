"""
Telegram-бот для водителей.

Функции:
  /start        — регистрация (водитель вводит ФИО один раз)
  /zp           — оклад / проценты / премия из Google Таблицы
  /osmotr       — чек-лист осмотра машины с фото и комментариями по пунктам,
                  автоматическое сравнение с предыдущим осмотром той же машины
  /cancel       — отменить текущий диалог

Запуск: python bot.py
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # подхватывает переменные из файла .env, если он есть

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import sheets_service
from image_compare import compare_images

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Локальная "база" привязки telegram_id -> ФИО
# --------------------------------------------------------------------------

def _load_drivers_db() -> dict:
    if config.DRIVERS_DB_PATH.exists():
        return json.loads(config.DRIVERS_DB_PATH.read_text(encoding="utf-8"))
    return {}


def _save_drivers_db(db: dict) -> None:
    config.DRIVERS_DB_PATH.write_text(
        json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_driver_fio(telegram_id: int) -> str | None:
    db = _load_drivers_db()
    return db.get(str(telegram_id))


def set_driver_fio(telegram_id: int, fio: str) -> None:
    db = _load_drivers_db()
    db[str(telegram_id)] = fio.strip()
    _save_drivers_db(db)


# --------------------------------------------------------------------------
# /start — регистрация
# --------------------------------------------------------------------------

REG_FIO = 1


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fio = get_driver_fio(update.effective_user.id)
    if fio:
        await update.message.reply_text(
            f"Здравствуйте, {fio}!\n\n"
            "Доступные команды:\n"
            "/zp — узнать оклад, проценты, премию\n"
            "/osmotr — пройти осмотр машины\n"
            "/whoami — изменить сохранённое ФИО"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Здравствуйте! Это бот для водителей.\n"
        "Для начала введите ваше ФИО (как в ведомости), например:\n"
        "Иванов Иван Иванович"
    )
    return REG_FIO


async def save_fio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fio = update.message.text.strip()
    set_driver_fio(update.effective_user.id, fio)
    await update.message.reply_text(
        f"Записал: {fio}\n\n"
        "Доступные команды:\n"
        "/zp — узнать оклад, проценты, премию\n"
        "/osmotr — пройти осмотр машины"
    )
    return ConversationHandler.END


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Введите новое ФИО:")
    return REG_FIO


# --------------------------------------------------------------------------
# /zp
# --------------------------------------------------------------------------

async def salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    fio = get_driver_fio(update.effective_user.id)
    if not fio:
        await update.message.reply_text("Сначала выполните /start и введите ваше ФИО.")
        return

    await update.message.reply_text("Ищу данные...")
    try:
        info = sheets_service.get_salary_info(fio)
    except sheets_service.DriverNotFound:
        await update.message.reply_text(
            f"Не нашёл в таблице строку с ФИО «{fio}».\n"
            "Проверьте, что ФИО указано так же, как в таблице, "
            "или обновите его через /whoami."
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при обращении к таблице зарплат")
        await update.message.reply_text(f"Не удалось получить данные: {exc}")
        return

    await update.message.reply_text(
        f"Данные по {fio}:\n\n"
        f"Оклад: {info['oklad']}\n"
        f"Проценты: {info['procenty']}\n"
        f"Премия: {info['premiya']}"
    )


# --------------------------------------------------------------------------
# /osmotr — осмотр машины
# --------------------------------------------------------------------------

(
    VEHICLE,
    ODOMETER,
    PHOTO,
    DAMAGE_YN,
    DAMAGE_DESC,
    EQUIPMENT,
    COMMENT,
) = range(10, 17)

YES_NO_KEYBOARD = ReplyKeyboardMarkup(
    [["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True
)


async def inspection_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fio = get_driver_fio(update.effective_user.id)
    if not fio:
        await update.message.reply_text("Сначала выполните /start и введите ваше ФИО.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["fio"] = fio
    context.user_data["photo_index"] = 0
    context.user_data["photo_results"] = []  # список dict по каждой фото-точке
    context.user_data["auto_flagged"] = []  # фото-точки, где сработало автосравнение
    context.user_data["damages"] = []  # повреждения, заявленные водителем
    context.user_data["equipment"] = {item["id"]: False for item in config.EQUIPMENT_ITEMS}

    await update.message.reply_text(
        "Начинаем осмотр. Введите гос. номер автомобиля:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return VEHICLE


async def inspection_vehicle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    vehicle_number = update.message.text.strip().upper().replace(" ", "")
    context.user_data["vehicle"] = vehicle_number

    session_dir = (
        config.INSPECTIONS_DIR
        / vehicle_number
        / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    context.user_data["session_dir"] = session_dir

    await update.message.reply_text("Введите текущий пробег (км), только цифрами:")
    return ODOMETER


async def inspection_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await update.message.reply_text(
            "Пробег должен быть числом, например 128450. Введите ещё раз:"
        )
        return ODOMETER

    context.user_data["odometer"] = int(raw)
    return await _ask_next_photo(update, context)


async def _ask_next_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["photo_index"]
    if idx >= len(config.PHOTO_POINTS):
        await update.message.reply_text(
            "Все фото получены.\n\nЕсть повреждения на машине?",
            reply_markup=YES_NO_KEYBOARD,
        )
        return DAMAGE_YN

    point = config.PHOTO_POINTS[idx]
    await update.message.reply_text(
        f"Фото {idx + 1}/{len(config.PHOTO_POINTS)}: {point['title']}",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PHOTO


async def inspection_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно именно фото. Пришлите фотографию.")
        return PHOTO

    idx = context.user_data["photo_index"]
    point = config.PHOTO_POINTS[idx]
    session_dir: Path = context.user_data["session_dir"]
    vehicle_number = context.user_data["vehicle"]

    photo_file = await update.message.photo[-1].get_file()
    dest_path = session_dir / f"{point['id']}.jpg"
    await photo_file.download_to_drive(custom_path=str(dest_path))

    previous_photo = _find_previous_point_photo(vehicle_number, session_dir, point["id"])
    compare_result = compare_images(dest_path, previous_photo)

    context.user_data["photo_results"].append(
        {
            "id": point["id"],
            "title": point["title"],
            "photo_path": str(dest_path),
            "similarity_to_previous": compare_result.get("similarity"),
            "possible_damage_detected": compare_result.get("is_damaged", False),
        }
    )
    if compare_result.get("is_damaged"):
        context.user_data["auto_flagged"].append(point["title"])
        await update.message.reply_text(
            "⚠️ Это фото заметно отличается от предыдущего осмотра — "
            "возможно повреждение. Отмечено в итоговой сводке."
        )

    context.user_data["photo_index"] += 1
    return await _ask_next_photo(update, context)


def _find_previous_point_photo(vehicle_number: str, current_session_dir: Path, point_id: str) -> Path | None:
    """Ищет фото point_id.jpg в последнем осмотре этой машины, кроме текущего."""
    vehicle_dir = config.INSPECTIONS_DIR / vehicle_number
    if not vehicle_dir.exists():
        return None

    session_dirs = sorted(
        (d for d in vehicle_dir.iterdir() if d.is_dir() and d != current_session_dir),
        key=lambda d: d.name,
        reverse=True,
    )
    for d in session_dirs:
        candidate = d / f"{point_id}.jpg"
        if candidate.exists():
            return candidate
    return None


# --- повреждения, заявленные водителем -------------------------------------

async def inspection_damage_yn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip().lower()
    if answer not in ("да", "нет"):
        await update.message.reply_text("Пожалуйста, используйте кнопки Да / Нет.")
        return DAMAGE_YN

    if answer == "нет":
        return await _start_equipment(update, context)

    await update.message.reply_text(
        "Опишите повреждение: что и где (например «скол на переднем бампере "
        "справа»). Фото можно приложить сразу — отправьте фото с подписью, "
        "или просто напишите текстом.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DAMAGE_DESC


async def inspection_damage_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session_dir: Path = context.user_data["session_dir"]
    damage_num = len(context.user_data["damages"]) + 1

    photo_path = None
    if update.message.photo:
        description = (update.message.caption or "").strip()
        photo_file = await update.message.photo[-1].get_file()
        dest_path = session_dir / f"damage_{damage_num}.jpg"
        await photo_file.download_to_drive(custom_path=str(dest_path))
        photo_path = str(dest_path)
    else:
        description = update.message.text.strip()

    if not description:
        description = "(без описания, см. фото)" if photo_path else ""

    if not description and not photo_path:
        await update.message.reply_text("Добавьте текст или фото повреждения.")
        return DAMAGE_DESC

    context.user_data["damages"].append({"description": description, "photo_path": photo_path})

    await update.message.reply_text("Есть ещё повреждения?", reply_markup=YES_NO_KEYBOARD)
    return DAMAGE_YN


# --- опросник комплектности (кнопки-галочки) --------------------------------

def _build_equipment_keyboard(selected: dict) -> InlineKeyboardMarkup:
    rows = []
    for item in config.EQUIPMENT_ITEMS:
        mark = "☑" if selected.get(item["id"]) else "☐"
        rows.append(
            [InlineKeyboardButton(f"{mark} {item['title']}", callback_data=f"eq:{item['id']}")]
        )
    rows.append([InlineKeyboardButton("Готово ✅", callback_data="eq:done")])
    return InlineKeyboardMarkup(rows)


async def _start_equipment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Отметьте, что есть в машине (нажимайте, чтобы поставить/снять галочку), "
        "затем нажмите «Готово»:",
        reply_markup=_build_equipment_keyboard(context.user_data["equipment"]),
    )
    return EQUIPMENT


async def inspection_equipment_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    _, value = query.data.split(":", 1)

    if value == "done":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Комментарии по осмотру (или отправьте /skip, если без комментария):"
        )
        return COMMENT

    equipment = context.user_data["equipment"]
    equipment[value] = not equipment.get(value, False)
    await query.edit_message_reply_markup(reply_markup=_build_equipment_keyboard(equipment))
    return EQUIPMENT


# --- комментарий и завершение -----------------------------------------------

async def inspection_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    comment = "" if update.message.text.strip() == "/skip" else update.message.text.strip()
    context.user_data["comment"] = comment
    return await _finish_inspection(update, context)


async def _finish_inspection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fio = context.user_data["fio"]
    vehicle_number = context.user_data["vehicle"]
    odometer = context.user_data["odometer"]
    session_dir: Path = context.user_data["session_dir"]
    photo_results = context.user_data["photo_results"]
    auto_flagged = context.user_data["auto_flagged"]
    damages = context.user_data["damages"]
    equipment = context.user_data["equipment"]
    comment = context.user_data.get("comment", "")

    equipment_titles = {item["id"]: item["title"] for item in config.EQUIPMENT_ITEMS}
    missing_equipment = [equipment_titles[k] for k, v in equipment.items() if not v]

    summary_payload = {
        "driver": fio,
        "vehicle": vehicle_number,
        "odometer_km": odometer,
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "photos": photo_results,
        "auto_flagged": auto_flagged,
        "driver_reported_damages": damages,
        "equipment": {equipment_titles[k]: v for k, v in equipment.items()},
        "comment": comment,
    }
    (session_dir / "results.json").write_text(
        json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    try:
        sheets_service.log_inspection(fio, vehicle_number, summary_payload)
    except Exception:
        logger.exception("Не удалось записать лог осмотра в таблицу")

    lines = [f"Осмотр завершён. Машина: {vehicle_number}, пробег: {odometer} км\n"]

    if damages:
        lines.append("Повреждения (со слов водителя):")
        for d in damages:
            mark = " 📷" if d["photo_path"] else ""
            lines.append(f"— {d['description']}{mark}")
    else:
        lines.append("Повреждений не заявлено.")

    if auto_flagged:
        lines.append("\n⚠️ Автосравнение с предыдущим осмотром — возможны отличия:")
        lines.extend(f"— {t}" for t in auto_flagged)

    if missing_equipment:
        lines.append("\nОтсутствует / не отмечено:")
        lines.extend(f"— {t}" for t in missing_equipment)
    else:
        lines.append("\nКомплектность: всё на месте.")

    if comment:
        lines.append(f"\nКомментарий: {comment}")

    await update.message.reply_text("\n".join(lines))

    if config.ADMIN_CHAT_ID and (damages or auto_flagged or missing_equipment):
        try:
            alert_parts = []
            if damages:
                alert_parts.append("повреждения заявлены")
            if auto_flagged:
                alert_parts.append("автосравнение нашло отличия")
            if missing_equipment:
                alert_parts.append("не хватает комплектности")
            await context.bot.send_message(
                chat_id=config.ADMIN_CHAT_ID,
                text=(
                    f"⚠️ Осмотр {vehicle_number} ({fio}), пробег {odometer} км: "
                    + ", ".join(alert_parts)
                ),
            )
        except Exception:
            logger.exception("Не удалось отправить уведомление админу")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Запуск бота
# --------------------------------------------------------------------------

def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("whoami", whoami)],
        states={REG_FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_fio)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    inspection_conv = ConversationHandler(
        entry_points=[CommandHandler("osmotr", inspection_start)],
        states={
            VEHICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, inspection_vehicle)],
            ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, inspection_odometer)],
            PHOTO: [MessageHandler(filters.PHOTO, inspection_photo)],
            DAMAGE_YN: [MessageHandler(filters.TEXT & ~filters.COMMAND, inspection_damage_yn)],
            DAMAGE_DESC: [MessageHandler(filters.PHOTO | filters.TEXT, inspection_damage_desc)],
            EQUIPMENT: [CallbackQueryHandler(inspection_equipment_toggle, pattern=r"^eq:")],
            COMMENT: [MessageHandler(filters.TEXT, inspection_comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(reg_conv)
    app.add_handler(inspection_conv)
    app.add_handler(CommandHandler("zp", salary))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
