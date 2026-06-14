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
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])
API_URL = f"https://api.telegram.org/bot{TOKEN}"

games: dict[str, dict] = {}
pinned_msg_id: int | None = None


# ── Восстановление из закрепа при старте ────────────────────
async def restore_from_pinned(app: Application) -> None:
    global games, pinned_msg_id
    try:
        # Получаем информацию о чате (включая закреплённое сообщение)
        resp = requests.get(
            f"{API_URL}/getChat",
            params={"chat_id": GROUP_CHAT_ID},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Ошибка получения чата: %s", data)
            return

        pinned = data["result"].get("pinned_message")
        if pinned and "text" in pinned:
            games = json.loads(pinned["text"])
            pinned_msg_id = pinned["message_id"]
            logger.info("Восстановлено %d игр из закрепа", len(games))
        else:
            logger.info("Закреплённое сообщение не найдено или пусто")
    except Exception as exc:
        logger.warning("Ошибка восстановления: %s", exc)


async def save_to_pinned(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохраняет таблицу в закреплённое сообщение (редактирует или создаёт новое)."""
    global pinned_msg_id
    text = json.dumps(games, ensure_ascii=False)
    try:
        if pinned_msg_id:
            # Редактируем существующее закреплённое
            resp = requests.post(
                f"{API_URL}/editMessageText",
                json={
                    "chat_id": GROUP_CHAT_ID,
                    "message_id": pinned_msg_id,
                    "text": text,
                },
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning("Не удалось отредактировать закреп: %s", data)
                # Если не вышло — создадим новое
                pinned_msg_id = None

        if not pinned_msg_id:
            # Отправляем новое сообщение и закрепляем его
            resp = requests.post(
                f"{API_URL}/sendMessage",
                json={
                    "chat_id": GROUP_CHAT_ID,
                    "text": text,
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                new_msg_id = data["result"]["message_id"]
                # Закрепляем
                requests.post(
                    f"{API_URL}/pinChatMessage",
                    json={
                        "chat_id": GROUP_CHAT_ID,
                        "message_id": new_msg_id,
                    },
                    timeout=10,
                )
                pinned_msg_id = new_msg_id
            else:
                logger.error("Ошибка создания закрепа: %s", data)
    except Exception as exc:
        logger.error("Ошибка сохранения в закреп: %s", exc)


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
        await save_to_pinned(context)
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
    app = Application.builder().token(TOKEN).post_init(restore_from_pinned).build()

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
