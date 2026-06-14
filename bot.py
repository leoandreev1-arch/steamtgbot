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
API_URL = f"https://api.telegram.org/bot{TOKEN}"

# ID канала-хранилища (обязателен для сохранения данных)
STORAGE_CHAT_ID = int(os.environ.get("STORAGE_CHAT_ID", "0"))

games: dict[str, dict] = {}
storage_msg_ids: list[int] = []   # ID сообщений-частей в канале
MAX_LEN = 4000                    # запас до лимита 4096


# ── Восстановление при старте ──────────────────────────────
async def restore_data(app: Application) -> None:
    global games, storage_msg_ids

    if not STORAGE_CHAT_ID:
        logger.warning("STORAGE_CHAT_ID не задан – данные не будут сохранены между перезапусками")
        return

    try:
        resp = requests.get(
            f"{API_URL}/getChatHistory",
            params={"chat_id": STORAGE_CHAT_ID, "limit": 10},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Ошибка получения истории канала: %s", data)
            return

        messages = data["result"]["messages"]
        if not messages:
            logger.info("Канал-хранилище пуст")
            return

        parts = {}
        for msg in messages:
            text = msg.get("text", "")
            if text.startswith("PART:"):
                try:
                    header, body = text.split("\n", 1)
                    part_num, total = (int(x) for x in header[5:].split("/"))
                    parts[part_num] = body
                except Exception:
                    continue

        if parts:
            sorted_parts = [parts[i] for i in sorted(parts.keys())]
            full_json = "".join(sorted_parts)
            games = json.loads(full_json)
            storage_msg_ids = [
                msg["message_id"]
                for msg in messages
                if msg.get("text", "").startswith("PART:")
            ]
            storage_msg_ids.sort()
            logger.info("Восстановлено %d игр из канала (%d частей)", len(games), len(parts))
        else:
            # Старый формат – одно сообщение
            first_msg = messages[0]
            if first_msg.get("text"):
                games = json.loads(first_msg["text"])
                storage_msg_ids = [first_msg["message_id"]]
                logger.info("Восстановлено %d игр (старый формат)", len(games))
    except Exception as exc:
        logger.warning("Ошибка восстановления из канала: %s", exc)


# ── Сохранение в канал ─────────────────────────────────────
async def save_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not STORAGE_CHAT_ID:
        logger.warning("STORAGE_CHAT_ID не задан – пропускаю сохранение")
        return

    global storage_msg_ids
    text = json.dumps(games, ensure_ascii=False)

    # Если влезает в одно сообщение
    if len(text) <= MAX_LEN:
        try:
            if storage_msg_ids:
                resp = requests.post(
                    f"{API_URL}/editMessageText",
                    json={
                        "chat_id": STORAGE_CHAT_ID,
                        "message_id": storage_msg_ids[-1],
                        "text": text,
                    },
                    timeout=10,
                )
                if resp.json().get("ok"):
                    # Удаляем лишние старые части
                    for msg_id in storage_msg_ids[:-1]:
                        requests.post(
                            f"{API_URL}/deleteMessage",
                            json={"chat_id": STORAGE_CHAT_ID, "message_id": msg_id},
                            timeout=5,
                        )
                    storage_msg_ids = [storage_msg_ids[-1]]
                    return
            # Создаём новое
            resp = requests.post(
                f"{API_URL}/sendMessage",
                json={"chat_id": STORAGE_CHAT_ID, "text": text},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                storage_msg_ids = [data["result"]["message_id"]]
        except Exception as exc:
            logger.error("Ошибка сохранения: %s", exc)
        return

    # ---- Многосообщенческое сохранение ----
    parts = []
    remaining = text
    while remaining:
        split_at = min(MAX_LEN, len(remaining))
        if split_at < len(remaining):
            last_comma = remaining.rfind(",", 0, split_at)
            if last_comma > MAX_LEN // 2:
                split_at = last_comma + 1
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]

    total = len(parts)
    new_ids = []

    for i, part in enumerate(parts, 1):
        header = f"PART:{i}/{total}\n"
        msg_text = header + part
        try:
            resp = requests.post(
                f"{API_URL}/sendMessage",
                json={"chat_id": STORAGE_CHAT_ID, "text": msg_text},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                new_ids.append(data["result"]["message_id"])
            else:
                logger.error("Ошибка отправки части %d: %s", i, data)
                return
        except Exception as exc:
            logger.error("Исключение при отправке части %d: %s", i, exc)
            return

    # Удаляем старые сообщения
    for old_id in storage_msg_ids:
        if old_id not in new_ids:
            try:
                requests.post(
                    f"{API_URL}/deleteMessage",
                    json={"chat_id": STORAGE_CHAT_ID, "message_id": old_id},
                    timeout=5,
                )
            except Exception:
                pass

    storage_msg_ids = new_ids


# ── Steam API ─────────────────────────────────────────────
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
            continue
    return None


# ── Таблица ───────────────────────────────────────────────
def build_short_table() -> str:
    if not games:
        return "📋 Список пока пуст."

    sorted_games = sorted(
        games.items(),
        key=lambda x: x[1].get("Дата", ""),
        reverse=True,
    )
    lines = ["<b>📋 Сравнение игр</b>\n"]
    for idx, (appid, row) in enumerate(sorted_games, 1):
        name = html.escape(row.get("Название", "?"))
        price = html.escape(row.get("Цена", "?"))
        link = row.get("Ссылка", f"https://store.steampowered.com/app/{appid}/")
        lines.append(f'{idx}. <a href="{link}">{name}</a> — {price}')
    lines.append(f"\nВсего игр: {len(games)}")
    return "\n".join(lines)


def get_sorted_games():
    return sorted(
        games.items(),
        key=lambda x: x[1].get("Дата", ""),
        reverse=True,
    )


def find_game_by_position(pos: int) -> str | None:
    sorted_games = get_sorted_games()
    if 1 <= pos <= len(sorted_games):
        return sorted_games[pos - 1][0]
    return None


def find_game_by_name(name_part: str) -> str | None:
    name_lower = name_part.lower()
    for appid, info in games.items():
        if name_lower in info.get("Название", "").lower():
            return appid
    return None


# ── Обработчики ──────────────────────────────────────────
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
        await save_data(context)
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


async def delete_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "Используйте: /delete <номер>, /delete <название>, /delete <appid> или /delete <ссылка>"
        )
        return

    target = args[0]
    appid_to_delete = None

    if target.isdigit():
        pos = int(target)
        if pos > 0 and pos <= len(games):
            appid_to_delete = find_game_by_position(pos)
        if not appid_to_delete and target in games:
            appid_to_delete = target

    if not appid_to_delete:
        match = re.search(r"/app/(\d+)", target)
        if match and match.group(1) in games:
            appid_to_delete = match.group(1)

    if not appid_to_delete:
        appid_to_delete = find_game_by_name(target)

    if not appid_to_delete:
        await update.message.reply_text("❌ Игра не найдена. Проверьте номер, название или appid.")
        return

    name = games[appid_to_delete].get("Название", appid_to_delete)
    del games[appid_to_delete]
    await save_data(context)
    await update.message.reply_text(f"🗑 Игра «{name}» удалена. Всего в списке: {len(games)}")


async def clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0].lower() not in ("yes", "да"):
        await update.message.reply_text("Для очистки списка введите /clear yes")
        return

    count = len(games)
    games.clear()
    await save_data(context)
    await update.message.reply_text(f"✅ Список очищен (удалено {count} игр)")


# ── Запуск ───────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(TOKEN).post_init(restore_data).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("show", show_table))
    app.add_handler(CommandHandler("s", show_table))
    app.add_handler(CommandHandler("delete", delete_game))
    app.add_handler(CommandHandler("d", delete_game))
    app.add_handler(CommandHandler("clear", clear_list))

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
