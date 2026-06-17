import html
import json
import logging
import os
import re
import time
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
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])   # ID твоей группы
API_URL = f"https://api.telegram.org/bot{TOKEN}"

games: dict[str, dict] = {}           # единый список игр
pinned_msg_id: int | None = None      # ID закреплённого сообщения (JSON)

KEY_DATE = "д"
KEY_NAME = "н"
KEY_PRICE = "ц"
ESTIMATED_BYTES_PER_GAME = 150
STEAM_DELAY = 2


# ═══════════════ Восстановление и сохранение в закреп ═════════════════
def load_games_from_pinned() -> dict[str, dict]:
    global pinned_msg_id
    try:
        resp = requests.get(
            f"{API_URL}/getChat",
            params={"chat_id": GROUP_CHAT_ID},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            pinned = data["result"].get("pinned_message")
            if pinned and "text" in pinned:
                pinned_msg_id = pinned["message_id"]
                return json.loads(pinned["text"])
    except Exception as exc:
        logger.warning("Ошибка загрузки из закрепа: %s", exc)
    return {}


def save_games_to_pinned() -> None:
    global pinned_msg_id
    text = json.dumps(games, ensure_ascii=False, separators=(',', ':'))
    try:
        if pinned_msg_id:
            resp = requests.post(
                f"{API_URL}/editMessageText",
                json={
                    "chat_id": GROUP_CHAT_ID,
                    "message_id": pinned_msg_id,
                    "text": text,
                },
                timeout=10,
            )
            if resp.json().get("ok"):
                return
            pinned_msg_id = None

        resp = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": GROUP_CHAT_ID, "text": text},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            new_id = data["result"]["message_id"]
            requests.post(
                f"{API_URL}/pinChatMessage",
                json={"chat_id": GROUP_CHAT_ID, "message_id": new_id},
                timeout=10,
            )
            pinned_msg_id = new_id
    except Exception as exc:
        logger.error("Ошибка сохранения в закреп: %s", exc)


# ═══════════════ Реакция 👌 ══════════════════════════════
def set_reaction(chat_id: int, message_id: int, emoji: str) -> None:
    try:
        requests.post(
            f"{API_URL}/setMessageReaction",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reaction": [{"type": "emoji", "emoji": emoji}]
            },
            timeout=10,
        )
    except Exception as exc:
        logger.error("Ошибка реакции: %s", exc)


# ═══════════════ Таблица ═════════════════════════════════
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


# ═══════════════ Steam API ═══════════════════════════════
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


def update_prices_for_all() -> bool:
    changed = False
    for appid, info in games.items():
        try:
            if appid != next(iter(games)):
                time.sleep(STEAM_DELAY)
            fresh = get_steam_data(appid)
            if fresh and fresh["price"] != info.get(KEY_PRICE):
                info[KEY_PRICE] = fresh["price"]
                changed = True
        except Exception:
            continue
    return changed


# ═══════════════ Вспомогательные функции ═════════════════
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


# ═══════════════ Обработчики команд ══════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    message_id = update.effective_message.message_id
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
        save_games_to_pinned()
        prices_changed = update_prices_for_all()
        if prices_changed:
            save_games_to_pinned()
        set_reaction(chat_id, message_id, "👌")


async def show_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        build_short_table(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def pin_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновляет закреплённую таблицу (не JSON, а красивую таблицу)."""
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
        await update.message.reply_text("❌ Не удалось закрепить таблицу. Проверьте права бота.")


async def show_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    total = len(games)
    if total == 0:
        await update.message.reply_text("📋 Список пуст. Лимит: ~26 игр в одном закреплённом сообщении.")
        return

    sample = json.dumps(games, ensure_ascii=False)
    current = len(sample.encode("utf-8"))
    remaining = max(0, (4000 - current) // ESTIMATED_BYTES_PER_GAME)

    lines = [
        f"📊 Игр в списке: <b>{total}</b>",
        f"📦 Занято: ~{current} / 4000 символов",
        f"➕ Ещё влезет: <b>~{remaining} игр</b>",
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

    name = games[appid_to_delete].get(KEY_NAME, appid_to_delete)
    del games[appid_to_delete]
    save_games_to_pinned()
    await update.message.reply_text(f"🗑 Игра «{name}» удалена. Всего в списке: {len(games)}")


async def clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0].lower() not in ("yes", "да"):
        await update.message.reply_text("Для очистки списка введите /clear yes")
        return

    count = len(games)
    games.clear()
    save_games_to_pinned()
    await update.message.reply_text(f"✅ Список очищен (удалено {count} игр)")


# ═══════════════ Запуск ══════════════════════════════════
async def post_init(app: Application) -> None:
    global games, pinned_msg_id
    games = load_games_from_pinned()
    logger.info("Загружено %d игр из закрепа", len(games))


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("show", show_table))
    app.add_handler(CommandHandler("s", show_table))
    app.add_handler(CommandHandler("pin", pin_table))
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
