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
STORAGE_CHAT_ID = int(os.environ["STORAGE_CHAT_ID"])
API_URL = f"https://api.telegram.org/bot{TOKEN}"

games: dict[str, dict] = {}
pinned_msg_id: int | None = None        # ID закреплённой таблицы (если создана через /pin)

KEY_DATE = "д"
KEY_NAME = "н"
KEY_PRICE = "ц"
ESTIMATED_BYTES_PER_GAME = 150


# ── Работа с каналом-хранилищем ──────────────────────────
def load_games() -> dict[str, dict]:
    try:
        resp = requests.get(
            f"{API_URL}/getChatHistory",
            params={"chat_id": STORAGE_CHAT_ID, "limit": 1},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok") and data["result"]["messages"]:
            text = data["result"]["messages"][0].get("text", "{}")
            all_data = json.loads(text)
            return all_data.get(str(GROUP_CHAT_ID), {})
    except Exception as exc:
        logger.warning("Ошибка загрузки: %s", exc)
    return {}


def save_games(games: dict[str, dict]) -> None:
    try:
        resp = requests.get(
            f"{API_URL}/getChatHistory",
            params={"chat_id": STORAGE_CHAT_ID, "limit": 1},
            timeout=10,
        )
        data = resp.json()
        all_data = {}
        if data.get("ok") and data["result"]["messages"]:
            try:
                all_data = json.loads(data["result"]["messages"][0].get("text", "{}"))
            except Exception:
                pass

        all_data[str(GROUP_CHAT_ID)] = games
        text = json.dumps(all_data, ensure_ascii=False)

        if data.get("ok") and data["result"]["messages"]:
            last_msg_id = data["result"]["messages"][0]["message_id"]
            requests.post(
                f"{API_URL}/editMessageText",
                json={
                    "chat_id": STORAGE_CHAT_ID,
                    "message_id": last_msg_id,
                    "text": text,
                },
                timeout=10,
            )
        else:
            requests.post(
                f"{API_URL}/sendMessage",
                json={"chat_id": STORAGE_CHAT_ID, "text": text},
                timeout=10,
            )
    except Exception as exc:
        logger.error("Ошибка сохранения: %s", exc)


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

            return {"name": name, "price": price}
        except Exception:
            continue
    return None


# ── Таблица ───────────────────────────────────────────────
def build_short_table() -> str:
    if not games:
        return "📋 Список пока пуст."

    sorted_games = sorted(
        games.items(),
        key=lambda x: x[1].get(KEY_DATE, x[1].get("Дата", "")),
        reverse=True,
    )
    lines = ["<b>📋 Сравнение игр</b>\n"]
    for idx, (appid, row) in enumerate(sorted_games, 1):
        name = html.escape(row.get(KEY_NAME, row.get("Название", "?")))
        price = html.escape(row.get(KEY_PRICE, row.get("Цена", "?")))
        link = f"https://store.steampowered.com/app/{appid}/"
        lines.append(f'{idx}. <a href="{link}">{name}</a> — {price}')
    lines.append(f"\nВсего игр: {len(games)}")
    return "\n".join(lines)


def get_sorted_games():
    return sorted(
        games.items(),
        key=lambda x: x[1].get(KEY_DATE, x[1].get("Дата", "")),
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
        real_name = info.get(KEY_NAME, info.get("Название", ""))
        if name_lower in real_name.lower():
            return appid
    return None


# ── Обработчики команд ───────────────────────────────────
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
            KEY_DATE: datetime.now().strftime("%Y-%m-%d %H:%M"),
            KEY_NAME: info["name"],
            KEY_PRICE: info["price"],
        }
        new_games += 1

    if new_games:
        save_games(games)
        await update.message.reply_text(
            f"✅ Игры добавлены. Всего в списке: {len(games)}\n"
            f"Посмотреть: /show\n"
            f"Закрепить таблицу: /pin"
        )


async def show_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        build_short_table(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def pin_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создаёт или обновляет закреплённую таблицу."""
    global pinned_msg_id
    table = build_short_table()

    try:
        if pinned_msg_id:
            await context.bot.edit_message_text(
                chat_id=GROUP_CHAT_ID,
                message_id=pinned_msg_id,
                text=table,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            msg = await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=table,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await msg.pin()
            pinned_msg_id = msg.message_id
        await update.message.reply_text("✅ Таблица закреплена.")
    except Exception as exc:
        logger.error("Ошибка закрепления: %s", exc)
        pinned_msg_id = None
        await update.message.reply_text("❌ Не удалось закрепить таблицу. Проверьте права бота.")


async def unpin_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Открепляет таблицу, если она есть."""
    global pinned_msg_id
    if not pinned_msg_id:
        await update.message.reply_text("Закреплённой таблицы нет.")
        return
    try:
        await context.bot.unpin_chat_message(chat_id=GROUP_CHAT_ID, message_id=pinned_msg_id)
        pinned_msg_id = None
        await update.message.reply_text("✅ Закреплённая таблица удалена.")
    except Exception as exc:
        logger.error("Ошибка открепления: %s", exc)
        await update.message.reply_text("❌ Не удалось открепить таблицу.")


async def show_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    total_games = len(games)
    if total_games == 0:
        await update.message.reply_text("📋 Список пуст. Лимит: ~26 игр в одном закреплённом сообщении.")
        return

    sample_json = json.dumps(games, ensure_ascii=False)
    current_bytes = len(sample_json.encode("utf-8"))
    remaining_games = max(0, (4000 - current_bytes) // ESTIMATED_BYTES_PER_GAME)

    lines = [
        f"📊 Игр в списке: <b>{total_games}</b>",
        f"📦 Занято: ~{current_bytes} / 4000 символов",
        f"➕ Ещё влезет: <b>~{remaining_games} игр</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
        await update.message.reply_text("❌ Игра не найдена.")
        return

    name = games[appid_to_delete].get(KEY_NAME, games[appid_to_delete].get("Название", appid_to_delete))
    del games[appid_to_delete]
    save_games(games)

    # Если закреплённая таблица есть – обновляем её автоматически
    if pinned_msg_id:
        await update_pinned_if_exists(context)

    await update.message.reply_text(f"🗑 Игра «{name}» удалена. Всего в списке: {len(games)}")


async def clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0].lower() not in ("yes", "да"):
        await update.message.reply_text("Для очистки списка введите /clear yes")
        return

    count = len(games)
    games.clear()
    save_games(games)

    if pinned_msg_id:
        await update_pinned_if_exists(context)

    await update.message.reply_text(f"✅ Список очищен (удалено {count} игр)")


async def update_pinned_if_exists(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вспомогательная функция – обновляет закреплённую таблицу, если она была создана."""
    global pinned_msg_id
    if not pinned_msg_id:
        return
    table = build_short_table()
    try:
        await context.bot.edit_message_text(
            chat_id=GROUP_CHAT_ID,
            message_id=pinned_msg_id,
            text=table,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.error("Ошибка обновления закреплённой таблицы: %s", exc)
        pinned_msg_id = None


# ── Запуск ───────────────────────────────────────────────
async def post_init(app: Application) -> None:
    global games, pinned_msg_id
    games = load_games()
    logger.info("Загружено %d игр из хранилища", len(games))

    # Попытаемся найти уже существующее закреплённое сообщение с таблицей
    try:
        resp = requests.get(
            f"{API_URL}/getChat",
            params={"chat_id": GROUP_CHAT_ID},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            pinned = data["result"].get("pinned_message")
            if pinned and "text" in pinned and "<b>📋 Сравнение игр</b>" in pinned["text"]:
                pinned_msg_id = pinned["message_id"]
                logger.info("Найдена закреплённая таблица (id=%d)", pinned_msg_id)
    except Exception:
        pass


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("show", show_table))
    app.add_handler(CommandHandler("s", show_table))
    app.add_handler(CommandHandler("pin", pin_table))
    app.add_handler(CommandHandler("unpin", unpin_table))
    app.add_handler(CommandHandler("limit", show_limit))
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
