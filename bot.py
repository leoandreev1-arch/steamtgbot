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
pinned_msg_ids: list[int] = []   # ID закреплённых сообщений-частей
MAX_LEN = 4000                   # запас до лимита 4096

# Поля в хранилище (короткие ключи)
KEY_DATE = "д"
KEY_NAME = "н"
KEY_PRICE = "ц"


# ── Восстановление из закрепа ──────────────────────────────
async def restore_data(app: Application) -> None:
    global games, pinned_msg_ids
    try:
        resp = requests.get(
            f"{API_URL}/getChatPinnedMessages",
            params={"chat_id": GROUP_CHAT_ID},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Ошибка получения закреплённых сообщений: %s", data)
            return

        messages = data["result"]["messages"]
        if not messages:
            logger.info("Нет закреплённых сообщений")
            return

        parts = {}
        single_candidate = None

        for msg in messages:
            text = msg.get("text", "")
            if text.startswith("PART:"):
                try:
                    header, body = text.split("\n", 1)
                    part_num, total = (int(x) for x in header[5:].split("/"))
                    parts[part_num] = body
                except Exception:
                    continue
            elif text:
                # Возможно, старая одиночная версия
                single_candidate = text

        if parts:
            sorted_parts = [parts[i] for i in sorted(parts.keys())]
            full_json = "".join(sorted_parts)
            games = json.loads(full_json)
            pinned_msg_ids = [m["message_id"] for m in messages if m.get("text", "").startswith("PART:")]
            logger.info("Восстановлено %d игр из закрепа (%d частей)", len(games), len(parts))
        elif single_candidate:
            # Старая одиночная версия
            try:
                games = json.loads(single_candidate)
                pinned_msg_ids = [messages[-1]["message_id"]]
                logger.info("Восстановлено %d игр (старое одиночное сообщение)", len(games))
            except Exception:
                logger.info("Закреплённое сообщение не содержит валидный JSON")
        else:
            logger.info("Закреплённые сообщения не содержат данных")

        # Конвертируем старые длинные ключи в новые короткие, если нужно
        convert_old_keys()
    except Exception as exc:
        logger.warning("Ошибка восстановления: %s", exc)


def convert_old_keys() -> None:
    """Преобразует старые длинные ключи в новые короткие."""
    changed = False
    for appid, info in games.items():
        # Проверяем, есть ли старые ключи
        if "Название" in info and KEY_NAME not in info:
            info[KEY_NAME] = info.pop("Название")
            changed = True
        if "Цена" in info and KEY_PRICE not in info:
            info[KEY_PRICE] = info.pop("Цена")
            changed = True
        if "Дата" in info and KEY_DATE not in info:
            info[KEY_DATE] = info.pop("Дата")
            changed = True
        # Удаляем больше не нужные ключи
        for old_key in ("Жанры", "Ссылка"):
            info.pop(old_key, None)
    if changed:
        logger.info("Ключи старых игр преобразованы в короткие")


# ── Сохранение в закреп (многосообщенческое) ──────────────
async def save_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    global pinned_msg_ids
    text = json.dumps(games, ensure_ascii=False)

    # Если влезает в одно сообщение
    if len(text) <= MAX_LEN:
        try:
            if pinned_msg_ids:
                # Пробуем отредактировать последнее
                resp = requests.post(
                    f"{API_URL}/editMessageText",
                    json={
                        "chat_id": GROUP_CHAT_ID,
                        "message_id": pinned_msg_ids[-1],
                        "text": text,
                    },
                    timeout=10,
                )
                if resp.json().get("ok"):
                    # Удаляем лишние старые закреплённые
                    for old_id in pinned_msg_ids[:-1]:
                        requests.post(
                            f"{API_URL}/deleteMessage",
                            json={"chat_id": GROUP_CHAT_ID, "message_id": old_id},
                            timeout=5,
                        )
                    pinned_msg_ids = [pinned_msg_ids[-1]]
                    return
            # Создаём новое и закрепляем
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
                # Удаляем все старые закреплённые
                for old_id in pinned_msg_ids:
                    requests.post(
                        f"{API_URL}/deleteMessage",
                        json={"chat_id": GROUP_CHAT_ID, "message_id": old_id},
                        timeout=5,
                    )
                pinned_msg_ids = [new_id]
        except Exception as exc:
            logger.error("Ошибка сохранения: %s", exc)
        return

    # ---- Многосообщенческое сохранение ----
    parts = []
    remaining = text
    while remaining:
        split_at = min(MAX_LEN, len(remaining))
        if split_at < len(remaining):
            # Ищем безопасное место для разрыва (после запятой)
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
                json={"chat_id": GROUP_CHAT_ID, "text": msg_text},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Ошибка отправки части %d: %s", i, data)
                return
            new_id = data["result"]["message_id"]
            requests.post(
                f"{API_URL}/pinChatMessage",
                json={"chat_id": GROUP_CHAT_ID, "message_id": new_id},
                timeout=10,
            )
            new_ids.append(new_id)
        except Exception as exc:
            logger.error("Исключение при отправке части %d: %s", i, exc)
            return

    # Удаляем старые закреплённые сообщения
    for old_id in pinned_msg_ids:
        if old_id not in new_ids:
            try:
                requests.post(
                    f"{API_URL}/deleteMessage",
                    json={"chat_id": GROUP_CHAT_ID, "message_id": old_id},
                    timeout=5,
                )
            except Exception:
                pass

    pinned_msg_ids = new_ids
    logger.info("Данные сохранены в %d закреплённых сообщениях", total)


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

            # Жанры не сохраняем, они не используются в /show
            return {
                "name": name,
                "price": price,
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
        key=lambda x: x[1].get(KEY_DATE, ""),
        reverse=True,
    )
    lines = ["<b>📋 Сравнение игр</b>\n"]
    for idx, (appid, row) in enumerate(sorted_games, 1):
        name = html.escape(row.get(KEY_NAME, "?"))
        price = html.escape(row.get(KEY_PRICE, "?"))
        link = f"https://store.steampowered.com/app/{appid}/"
        lines.append(f'{idx}. <a href="{link}">{name}</a> — {price}')
    lines.append(f"\nВсего игр: {len(games)}")
    return "\n".join(lines)


def get_sorted_games():
    return sorted(
        games.items(),
        key=lambda x: x[1].get(KEY_DATE, ""),
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
        if name_lower in info.get(KEY_NAME, "").lower():
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
        # Сохраняем только короткие ключи
        games[appid] = {
            KEY_DATE: datetime.now().strftime("%Y-%m-%d %H:%M"),
            KEY_NAME: info["name"],
            KEY_PRICE: info["price"],
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

    name = games[appid_to_delete].get(KEY_NAME, appid_to_delete)
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
