import html
import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, Any, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# =========================== Конфигурация ===========================
TOKEN = os.environ["TOKEN"]
DATA_FILE = "games.json"
STEAM_API_TIMEOUT = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================== Работа с файлом ===========================
def load_games() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Ошибка загрузки {DATA_FILE}: {e}")
        return {}

def save_games(games: Dict[str, Any]) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(games, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error(f"Ошибка сохранения {DATA_FILE}: {e}")

# =========================== Парсинг Steam (асинхронно) ===========================
async def fetch_steam_data(session: aiohttp.ClientSession, appid: str) -> Optional[Dict[str, str]]:
    regions = [
        ("ru", "russian", "₽"),
        ("us", "russian", "USD"),
        ("us", "english", "USD"),
    ]

    for cc, lang, currency in regions:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={cc}&l={lang}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=STEAM_API_TIMEOUT)) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if not data or str(appid) not in data or not data[str(appid)].get("success"):
                continue

            game_data = data[str(appid)]["data"]
            name = game_data.get("name", "Без названия")

            price_info = game_data.get("price_overview")
            if price_info:
                amount = price_info["final"] / 100.0
                price = f"{amount:.2f} {currency}"
            else:
                price = "Бесплатно" if game_data.get("is_free") else "Нет цены"

            genres = ", ".join(g["description"] for g in game_data.get("genres", [])) or "Не указаны"
            description = game_data.get("short_description", "—")
            if lang != "russian":
                description += " (описание на английском)"

            return {"name": name, "price": price, "genres": genres, "description": description}
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
            logger.warning(f"Ошибка {cc}/{lang} для {appid}: {e}")
            continue
    return None

# =========================== Форматирование ===========================
def escape(text: str) -> str:
    return html.escape(text)

def format_game_card(game: Dict[str, str], appid: str) -> str:
    return (
        f"🎮 <b>{escape(game['name'])}</b>\n"
        f"💰 Цена: {escape(game['price'])}\n"
        f"🏷 Жанры: {escape(game['genres'])}\n"
        f"📝 {escape(game['description'])}\n"
        f"🔗 <a href='https://store.steampowered.com/app/{appid}/'>Ссылка</a>"
    )

def format_table(games: Dict[str, Any]) -> str:
    if not games:
        return "📭 Таблица пуста. Отправьте ссылку на игру Steam."

    items = sorted(games.items(), key=lambda x: x[1].get("Дата обновления", ""), reverse=True)
    blocks = [
        (
            f"🎮 <b>{escape(row.get('Название', '?'))}</b>\n"
            f"💰 Цена: {escape(row.get('Цена', '?'))}\n"
            f"🏷 Жанры: {escape(row.get('Жанры', '?'))}\n"
            f"📝 <i>{escape(row.get('Описание', '—'))}</i>"
        )
        for _, row in items
    ]
    header = f"<b>📊 Игры в списке ({len(games)} шт.)</b>\n"
    return header + "\n" + "—" * 25 + "\n".join(blocks)

# =========================== Обработчики ===========================
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для учёта игр Steam.\n\n"
        "📌 Отправь ссылку на игру (store.steampowered.com/app/...)\n"
        "📋 /table или /t – показать список\n"
        "💾 /backup – скачать резервную копию\n"
        "🔄 /restore – восстановить из файла (отправь JSON после команды)",
        parse_mode=ParseMode.HTML,
    )

async def table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    games = context.bot_data.get("games", {})
    await update.message.reply_text(format_table(games), parse_mode=ParseMode.HTML)

async def backup(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(DATA_FILE):
        await update.message.reply_text("❌ Нет данных для резервного копирования.")
        return
    with open(DATA_FILE, "rb") as f:
        await update.message.reply_document(document=f, filename="games_backup.json", caption="💾 Резервная копия")

async def restore(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Отправьте JSON-файл (резервную копию) для восстановления.")

async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    appids = re.findall(r"https?://store\.steampowered\.com/app/(\d+)", text)
    if not appids:
        return

    session = context.bot_data.get("aiohttp_session")
    if not session:
        await update.message.reply_text("❌ Внутренняя ошибка: нет HTTP-сессии.")
        return

    games = context.bot_data["games"]
    for appid in set(appids):
        info = await fetch_steam_data(session, appid)
        if not info:
            await update.message.reply_text(f"❌ Не удалось получить данные для {appid}")
            continue

        games[appid] = {
            "Дата обновления": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Название": info["name"],
            "Цена": info["price"],
            "Жанры": info["genres"],
            "Описание": info["description"],
            "Ссылка": f"https://store.steampowered.com/app/{appid}/",
        }
        save_games(games)

        await update.message.reply_text(
            f"{format_game_card(info, appid)}\n\n"
            f"<i>Таблица обновлена. Всего игр: {len(games)}</i>\n"
            f"Показать список: /table",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".json") or doc.file_size > 1_000_000:
        return

    file = await doc.get_file()
    try:
        content = await file.download_as_bytearray()
        data = json.loads(content.decode("utf-8"))
        if not isinstance(data, dict):
            await update.message.reply_text("❌ Неверный формат: ожидается JSON-объект.")
            return

        # Базовая валидация
        for appid, game_data in data.items():
            if not isinstance(appid, str) or not isinstance(game_data, dict):
                await update.message.reply_text("❌ Неверная структура данных.")
                return
            required = {"Название", "Цена", "Жанры", "Описание", "Ссылка", "Дата обновления"}
            if not required.issubset(game_data.keys()):
                await update.message.reply_text(f"❌ В игре {appid} отсутствуют обязательные поля.")
                return

        context.bot_data["games"] = data
        save_games(data)
        await update.message.reply_text(f"✅ Таблица восстановлена! Игр: {len(data)}")
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Файл не является корректным JSON.")
    except Exception as e:
        logger.exception("Ошибка восстановления")
        await update.message.reply_text(f"❌ Ошибка: {e}")

# =========================== Запуск ===========================
async def post_init(app: Application):
    app.bot_data["aiohttp_session"] = aiohttp.ClientSession()
    app.bot_data["games"] = load_games()

async def post_shutdown(app: Application):
    session = app.bot_data.get("aiohttp_session")
    if session:
        await session.close()

def main():
    app = Application.builder().token(TOKEN).build()
    app.post_init = post_init
    app.post_shutdown = post_shutdown

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("table", table))
    app.add_handler(CommandHandler("t", table))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("restore", restore))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), handle_document))

    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8443))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{webhook_url}/{TOKEN}")
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
