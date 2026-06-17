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
STORAGE_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])   # твоя переменная – это хранилище
API_URL = f"https://api.telegram.org/bot{TOKEN}"

games: dict[str, dict] = {}           # единый список игр
pinned_messages: dict[int, int] = {}  # ID закреплённых таблиц по chat_id
storage_msg_id: int | None = None

KEY_DATE = "д"
KEY_NAME = "н"
KEY_PRICE = "ц"
ESTIMATED_BYTES_PER_GAME = 150
STEAM_DELAY = 2


# ═══════════════ Работа с хранилищем ═════════════════════
def load_all_data() -> dict[str, dict]:
    """Загружает игры из последнего валидного JSON-сообщения в канале."""
    global storage_msg_id
    try:
        # Проверяем до 20 последних сообщений, чтобы точно найти JSON
        resp = requests.get(
            f"{API_URL}/getChatHistory",
            params={"chat_id": STORAGE_CHAT_ID, "limit": 20},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok") or not data["result"]["messages"]:
            logger.info("Хранилище пусто")
            return {}

        logger.info("Проверяю %d сообщений в хранилище", len(data["result"]["messages"]))
        for msg in data["result"]["messages"]:
            text = msg.get("text", "").strip()
            msg_id = msg["message_id"]
            if not text:
                logger.debug("Сообщение %d пустое, пропускаю", msg_id)
                continue
            try:
                raw = json.loads(text)
            except Exception:
                logger.debug("Сообщение %d не JSON, пропускаю", msg_id)
                continue
            if not isinstance(raw, dict) or not raw:
                logger.debug("Сообщение %d не словарь или пустой, пропускаю", msg_id)
                continue

            # Нашли подходящее сообщение
            storage_msg_id = msg_id
            raw.pop("pinned_chat_id", None)  # удаляем служебный ключ, если есть

            # Определяем формат: плоский список игр (ключи – appid)
            if all(isinstance(k, str) and k.isdigit() and len(k) >= 5 for k in raw):
                logger.info("Загружено %d игр из сообщения %d (плоский формат)", len(raw), msg_id)
                return raw

            # Возможно, старый формат {chat_id: {appid: данные}}
            merged = {}
            for chat_games in raw.values():
                if isinstance(chat_games, dict):
                    for appid, info in chat_games.items():
                        if isinstance(appid, str) and appid.isdigit() and len(appid) >= 5:
                            merged[appid] = info
            if merged:
                logger.info("Загружено %d игр из сообщения %d (объединённый формат)", len(merged), msg_id)
                return merged

        logger.info("Не найдено подходящего JSON в хранилище")
        return {}
    except Exception as exc:
        logger.warning("Ошибка загрузки из хранилища: %s", exc)
        return {}


def save_all_data() -> None:
    global storage_msg_id
    text = json.dumps(games, ensure_ascii=False, separators=(',', ':'))
    try:
        if storage_msg_id:
            # Пробуем отредактировать известное сообщение
            resp = requests.post(
                f"{API_URL}/editMessageText",
                json={
                    "chat_id": STORAGE_CHAT_ID,
                    "message_id": storage_msg_id,
                    "text": text,
                },
                timeout=10,
            )
            if resp.json().get("ok"):
                return
            logger.warning("Не удалось отредактировать сообщение %d, создаю новое", storage_msg_id)

        # Создаём новое сообщение
        resp = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": STORAGE_CHAT_ID, "text": text},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            new_id = data["result"]["message_id"]
            # Удаляем все остальные сообщения в канале, оставляя только новое
            try:
                history = requests.get(
                    f"{API_URL}/getChatHistory",
                    params={"chat_id": STORAGE_CHAT_ID, "limit": 50},
                    timeout=10,
                ).json()
                if history.get("ok"):
                    for msg in history["result"]["messages"]:
                        if msg["message_id"] != new_id:
                            requests.post(
                                f"{API_URL}/deleteMessage",
                                json={"chat_id": STORAGE_CHAT_ID, "message_id": msg["message_id"]},
                                timeout=5,
                            )
            except Exception as exc:
                logger.warning("Ошибка при очистке старых сообщений: %s", exc)
            storage_msg_id = new_id
    except Exception as exc:
        logger.error("Ошибка сохранения: %s", exc)


# ═══════════════ Установка реакции ══════════════════════
def set_reaction(chat_id: int, message_id: int, emoji: str) -> None:
    """Ставит реакцию на сообщение."""
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
        logger.error("Ошибка установки реакции: %s", exc)


# ═══════════════ Таблица (общая для всех чатов) ═════════════
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
    """Обновляет цены для всех игр. Возвращает True, если что-то изменилось."""
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


async def update_pinned_if_exists(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обновляет закреплённую таблицу в конкретном чате, если она была создана."""
    pinned_id = pinned_messages.get(chat_id)
    if pinned_id:
        try:
            table = build_short_table()
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=pinned_id,
                text=table,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pinned_messages.pop(chat_id, None)

    try:
        resp = requests.get(
            f"{API_URL}/getChat",
            params={"chat_id": chat_id},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            pinned = data["result"].get("pinned_message")
            if pinned and "text" in pinned and "📋 Сравнение игр" in pinned["text"]:
                table = build_short_table()
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=pinned["message_id"],
                    text=table,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                pinned_messages[chat_id] = pinned["message_id"]
    except Exception:
        pass


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
        save_all_data()
        prices_changed = update_prices_for_all()
        if prices_changed:
            save_all_data()
        await update_pinned_if_exists(chat_id, context)
        for cid in list(pinned_messages.keys()):
            if cid != chat_id:
                await update_pinned_if_exists(cid, context)
        set_reaction(chat_id, message_id, "👌")


async def show_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        build_short_table(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def pin_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создаёт или обновляет закреплённую таблицу в текущем чате."""
    chat_id = update.effective_chat.id
    table = build_short_table()

    if chat_id not in pinned_messages:
        try:
            resp = requests.get(
                f"{API_URL}/getChat",
                params={"chat_id": chat_id},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                pinned = data["result"].get("pinned_message")
                if pinned and "text" in pinned and "📋 Сравнение игр" in pinned["text"]:
                    pinned_messages[chat_id] = pinned["message_id"]
        except Exception:
            pass

    pinned_id = pinned_messages.get(chat_id)
    try:
        if pinned_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=pinned_id,
                text=table,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=table,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await msg.pin()
            pinned_messages[chat_id] = msg.message_id
        await update.message.reply_text("✅ Таблица закреплена.")
    except Exception as exc:
        logger.error("Ошибка закрепления в чате %d: %s", chat_id, exc)
        pinned_messages.pop(chat_id, None)
        await update.message.reply_text("❌ Не удалось закрепить таблицу. Проверьте права бота.")


async def unpin_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    current_pinned = pinned_messages.get(chat_id)
    if not current_pinned:
        await update.message.reply_text("Закреплённой таблицы нет.")
        return
    try:
        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=current_pinned)
        pinned_messages.pop(chat_id, None)
        await update.message.reply_text("✅ Закреплённая таблица удалена.")
    except Exception as exc:
        logger.error("Ошибка открепления: %s", exc)
        await update.message.reply_text("❌ Не удалось открепить таблицу.")


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
    save_all_data()
    for cid in list(pinned_messages.keys()):
        await update_pinned_if_exists(cid, context)
    await update.message.reply_text(f"🗑 Игра «{name}» удалена. Всего в списке: {len(games)}")


async def clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0].lower() not in ("yes", "да"):
        await update.message.reply_text("Для очистки списка введите /clear yes")
        return

    count = len(games)
    games.clear()
    save_all_data()
    for cid in list(pinned_messages.keys()):
        await update_pinned_if_exists(cid, context)
    await update.message.reply_text(f"✅ Список очищен (удалено {count} игр)")


# ═══════════════ Запуск ══════════════════════════════════
async def post_init(app: Application) -> None:
    global games, storage_msg_id
    games = load_all_data()
    logger.info("Загружено %d игр из хранилища", len(games))


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
