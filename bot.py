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

KEY_DATE = "д"
KEY_NAME = "н"
KEY_PRICE = "ц"
KEY_MODES = "р"
ESTIMATED_BYTES_PER_GAME = 200


# ── Восстановление из закрепа ──────────────────────────────
async def restore_from_pinned(app: Application) -> None:
    global games, pinned_msg_id
    try:
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
            logger.info("Закреплённое сообщение не найдено")
    except Exception as exc:
        logger.warning("Ошибка восстановления: %s", exc)


async def save_to_pinned(context: ContextTypes.DEFAULT_TYPE) -> None:
    global pinned_msg_id
    text = json.dumps(games, ensure_ascii=False)
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
            else:
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

            # Режимы и лимиты
            categories = g.get("categories", [])
            player_limit = None
            has_multi = False
            has_coop = False

            for cat in categories:
                cat_id = cat.get("id", 0)
                # Мультиплеерные категории
                if cat_id in (1, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48):
                    has_multi = True
                elif cat_id == 49 or cat_id == 9:
                    has_coop = True

                # Лимиты игроков
                if cat_id == 2:
                    player_limit = 2
                elif cat_id == 3:
                    player_limit = 4
                elif cat_id == 4:
                    player_limit = 6
                elif cat_id == 5:
                    player_limit = 8
                elif cat_id == 6:
                    player_limit = 12
                elif cat_id == 7:
                    player_limit = 16
                elif cat_id == 8:
                    player_limit = 24

            # Формируем короткую метку для /show
            if has_multi:
                if player_limit:
                    short_mode = f"до {player_limit}👥"
                else:
                    short_mode = "👥"
            else:
                short_mode = "1👤"   # <-- ИСПРАВЛЕНО

            # Текстовое описание для ответа при добавлении
            desc_parts = []
            if has_multi:
                if player_limit:
                    desc_parts.append(f"Мультиплеер (до {player_limit} игроков)")
                else:
                    desc_parts.append("Мультиплеер")
            if has_coop:
                desc_parts.append("Кооператив")
            if not desc_parts:
                desc_parts.append("Одиночная")

            return {
                "name": name,
                "price": price,
                "short_mode": short_mode,
                "desc_mode": ", ".join(desc_parts),
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
        key=lambda x: x[1].get(KEY_DATE, x[1].get("Дата", "")),
        reverse=True,
    )
    lines = ["<b>📋 Сравнение игр</b>\n"]
    for idx, (appid, row) in enumerate(sorted_games, 1):
        name = html.escape(row.get(KEY_NAME, row.get("Название", "?")))
        price = html.escape(row.get(KEY_PRICE, row.get("Цена", "?")))
        link = f"https://store.steampowered.com/app/{appid}/"
        mode_str = row.get(KEY_MODES, "1👤")   # <-- ИСПРАВЛЕНО
        lines.append(f'{idx}. <a href="{link}">{name}</a> — {price} {mode_str}')
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
            KEY_DATE: datetime.now().strftime("%Y-%m-%d %H:%M"),
            KEY_NAME: info["name"],
            KEY_PRICE: info["price"],
            KEY_MODES: info.get("short_mode", "1👤"),   # <-- ИСПРАВЛЕНО
        }
        new_games += 1

        await update.message.reply_text(
            f"✅ Игра добавлена\n"
            f"🎮 {info['name']}\n"
            f"💰 Цена: {info['price']}\n"
            f"👥 {info.get('desc_mode', 'Одиночная')}",
        )

    if new_games:
        await save_to_pinned(context)
        await update.message.reply_text(
            f"Всего в списке: {len(games)}\n"
            f"Посмотреть: /show"
        )


async def show_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        build_short_table(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def show_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    total_games = len(games)
    if total_games == 0:
        await update.message.reply_text("📋 Список пуст.")
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
    await save_to_pinned(context)
    await update.message.reply_text(f"🗑 Игра «{name}» удалена. Всего в списке: {len(games)}")


async def clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or args[0].lower() not in ("yes", "да"):
        await update.message.reply_text("Для очистки списка введите /clear yes")
        return

    count = len(games)
    games.clear()
    await save_to_pinned(context)
    await update.message.reply_text(f"✅ Список очищен (удалено {count} игр)")


# ── Запуск ───────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(TOKEN).post_init(restore_from_pinned).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("show", show_table))
    app.add_handler(CommandHandler("s", show_table))
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
