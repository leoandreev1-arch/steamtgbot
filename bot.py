import html
import json
import logging
import os
import re
from datetime import datetime

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.environ["TOKEN"]
STORAGE_CHAT_ID = int(os.environ["STORAGE_CHAT_ID"])

games: dict[str, dict] = {}
storage_msg_id: int | None = None


# ── Работа с хранилищем ──────────────────────────────────────
async def restore_from_storage(app: Application) -> None:
    global games, storage_msg_id
    try:
        async for msg in app.bot.get_chat_history(chat_id=STORAGE_CHAT_ID, limit=1):
            if msg.text:
                games = json.loads(msg.text)
                storage_msg_id = msg.message_id
                logger.info("Восстановлено %d игр из хранилища", len(games))
            else:
                logger.warning("Последнее сообщение в хранилище не содержит текста")
    except Exception as exc:
        logger.warning("Не удалось восстановить данные: %s", exc)


async def save_to_storage(context: ContextTypes.DEFAULT_TYPE) -> None:
    global storage_msg_id
    text = json.dumps(games, ensure_ascii=False)
    try:
        if storage_msg_id:
            await context.bot.edit_message_text(
                chat_id=STORAGE_CHAT_ID, message_id=storage_msg_id, text=text
            )
        else:
            msg = await context.bot.send_message(chat_id=STORAGE_CHAT_ID, text=text)
            storage_msg_id = msg.message_id
    except Exception as exc:
        logger.error("Ошибка сохранения: %s", exc)


# ── Steam API ─────────────────────────────────────────────────
def get_steam_data(appid: str) -> dict | None:
    """
    Пытается получить данные об игре через Steam Store API,
    перебирая комбинации региона и языка.
    Возвращает словарь с ключами name, price, genres, link или None.
    """
    for region, lang, currency_label in [
        ("ru", "russian", "₽"),
        ("us", "russian", "USD"),
        ("us", "english", "USD"),
    ]:
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={region}&l={lang}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data or str(appid) not in data or not data[str(appid)]["success"]:
                continue

            g = data[str(appid)]["data"]
            is_demo = g.get("is_demo", False) or g.get("type", "") == "demo"
            name = g.get("name", "Без названия")
            price_info = g.get("price_overview")

            if is_demo:
                price = "Демо"
            elif price_info:
                amount = price_info["final"] / 100
                price = f"{amount:.2f} {currency_label}" if currency_label != "₽" else f"{amount:.2f} ₽"
                if currency_label == "USD":
                    price += " (нет в РФ)"
            elif g.get("is_free"):
                price = "Бесплатно"
            else:
                price = "Нет цены"

            genres_list = [x["description"] for x in g.get("genres", [])]
            genres = ", ".join(genres_list) if genres_list else "Не указаны"

            return {
                "name": name,
                "price": price,
                "genres": genres,
                "link": f"https://store.steampowered.com/app/{appid}/",
            }
        except Exception:
            logger.debug("Ошибка при запросе %s (region=%s, lang=%s)", appid, region, lang)
            continue
    return None


# ── Таблица ───────────────────────────────────────────────────
def build_short_table() -> str:
    if not games:
        return "📋 Список пока пуст."

    # Сортируем по дате добавления (самые свежие сверху)
    sorted_games = sorted(games.items(), key=lambda x: x[1].get("Дата", ""), reverse=True)
    lines = ["<b>📋 Сравнение игр</b>\n"]
    for appid, row in sorted_games:
        name = html.escape(row.get("Название", "?"))
        price = html.escape(row.get("Цена", "?"))
        link = row.get("Ссылка", f"https://store.steampowered.com/app/{appid}/")
        lines.append(f'• <a href="{link}">{name}</a> — {price}')

    lines.append(f"\nВсего игр: {len(games)}")
    return "\n".join(lines)


# ── Обработчики ───────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    appids = set(re.findall(r"https?://store\.steampowered\.com/app/(\d+)", text))
    if not appids:
        return

    new_games = 0
    for appid in appids:
        info = get_steam_data(appid)
        if info is None:
            await update.message.reply_text(f"❌ Не удалось найти данные об игре {appid}")
            continue
        games[appid] = {
            "Дата": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Название": info["name"],
            "Цена": info["price"],
            "Жанры": info["genres"],
            "Ссылка": info["link"],
        }
        new_games += 1

    if new_games:
        await save_to_storage(context)
        await update.message.reply_text(
            f"✅ Игры добавлены. Всего в списке: {len(games)}\n"
            f"Ввесь список: /show"
        )


async def show_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        build_short_table(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── Точка входа ───────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(TOKEN).post_init(restore_from_storage).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("show", show_table))
    app.add_handler(CommandHandler("s", show_table))

    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8443))
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,
            webhook_url=f"{webhook_url}/{TOKEN}",
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
