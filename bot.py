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

TOKEN = os.environ["TOKEN"]
STORAGE_CHAT_ID = int(os.environ["STORAGE_CHAT_ID"])
API_URL = f"https://api.telegram.org/bot{TOKEN}"

games: dict[str, dict] = {}
storage_msg_id: int | None = None


# ── Восстановление через HTTP-запрос к Telegram API ─────────
async def restore_from_storage(app: Application) -> None:
    global games, storage_msg_id
    try:
        # Получаем последнее сообщение из канала через прямой запрос к API
        resp = requests.get(
            f"{API_URL}/getChatHistory",
            params={
                "chat_id": STORAGE_CHAT_ID,
                "limit": 1,
            },
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Ошибка API: %s", data)
            return

        messages = data["result"]["messages"]
        if messages:
            msg = messages[0]
            if "text" in msg:
                games = json.loads(msg["text"])
                storage_msg_id = msg["message_id"]
                logger.info("Восстановлено %d игр из хранилища", len(games))
            else:
                logger.warning("Последнее сообщение не содержит текста")
        else:
            logger.info("Хранилище пусто")
    except Exception as exc:
        logger.warning("Не удалось восстановить: %s", exc)


async def save_to_storage(context: ContextTypes.DEFAULT_TYPE) -> None:
    global storage_msg_id
    text = json.dumps(games, ensure_ascii=False)
    try:
        # Всегда отправляем новое сообщение (проще и надёжнее)
        resp = requests.post(
            f"{API_URL}/sendMessage",
            json={
                "chat_id": STORAGE_CHAT_ID,
                "text": text,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            storage_msg_id = data["result"]["message_id"]
        else:
            logger.error("Ошибка отправки: %s", data)
    except Exception as exc:
        logger.error("Ошибка сохранения: %s", exc)


# ── Steam API ─────────────────────────────────────────────────
def get_steam_data(appid: str) -> dict | None:
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
            logger.debug("Ошибка Steam API: %s", appid)
            continue
    return None


# ── Короткая таблица ─────────────────────────────────────────
def build_short_table() -> str:
    if not games:
        return "📋 Список пока пуст."

    sorted_games = sorted(
        games.items(),
        key=lambda x: x[1].get("Дата", ""),
        reverse=True,
    )
    lines = ["<b>📋 Сравнение игр</b>\n"]
    for appid, row in sorted_games:
        name = html.escape(row.get("Название", "?"))
        price = html.escape(row.get("Цена", "?"))
        link = row.get("Ссылка", f"https://store.steampowered.com/app/{appid}/")
        lines.append(f'• <a href="{link}">{name}</a> — {price}')
    lines.append(f"\nВсего игр: {len(games)}")
    return "\n".join(lines)


# ── Обработчики ──────────────────────────────────────────────
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
            f"Посмотреть: /show"
        )


async def show_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        build_short_table(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── Точка входа ──────────────────────────────────────────────
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
