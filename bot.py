import re, os, json, logging, requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TOKEN"]
DATA_FILE = "games.json"

# Загружаем сохранённую таблицу при старте
def load_games():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_games(games):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(games, f, ensure_ascii=False, indent=2)

games = load_games()

def get_steam_data(appid):
    for region, lang, currency_label in [
        ("ru", "russian", "₽"),
        ("ru", "english", "₽"),
        ("us", "english", "USD")
    ]:
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={region}&l={lang}"
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            if not data or str(appid) not in data or not data[str(appid)]["success"]:
                continue
            g = data[str(appid)]["data"]
            name = g.get("name", "Без названия")
            price_info = g.get("price_overview")
            if price_info:
                amount = price_info["final"] / 100
                if currency_label == "₽":
                    price = f"{amount:.2f} ₽"
                else:
                    price = f"{amount:.2f} USD (недоступна в РФ)"
            else:
                price = "Бесплатно" if g.get("is_free") else "Нет цены"
            genres_list = [x["description"] for x in g.get("genres", [])]
            genres = ", ".join(genres_list) if genres_list else "Не указаны"
            description = g.get("short_description", "—")
            if lang == "english" and region == "ru":
                description += " (описание на английском)"
            return {
                "name": name,
                "price": price,
                "genres": genres,
                "description": description
            }
        except Exception as e:
            logging.error(f"Steam error: {e}")
            continue
    return None

def format_table():
    if not games:
        return "Таблица пока пуста. Киньте ссылку на игру Steam."

    sorted_games = sorted(games.items(), key=lambda x: x[1].get("Дата обновления", ""), reverse=True)
    blocks = []
    for appid, row in sorted_games:
        name = row.get("Название", "?")
        price = row.get("Цена", "?")
        genres = row.get("Жанры", "?")
        desc = row.get("Описание", "—")
        block = (
            f"🎮 <b>{name}</b>\n"
            f"💰 Цена: {price}\n"
            f"🏷 Жанры: {genres}\n"
            f"📝 <i>{desc}</i>"
        )
        blocks.append(block)

    header = f"<b>📊 Игры в списке ({len(games)} шт.)</b>\n"
    separator = "\n" + "—" * 25 + "\n"
    return header + separator.join(blocks)

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    pattern = r"https?://store\.steampowered\.com/app/(\d+)"
    appids = re.findall(pattern, text)
    if not appids:
        return

    for appid in set(appids):
        info = get_steam_data(appid)
        if not info:
            await update.message.reply_text(f"❌ Не удалось получить данные для приложения {appid}")
            continue

        row = {
            "Дата обновления": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Название": info["name"],
            "Цена": info["price"],
            "Жанры": info["genres"],
            "Описание": info["description"],
            "Ссылка": f"https://store.steampowered.com/app/{appid}/"
        }

        games[appid] = row
        save_games(games)

        reply = (
            f"🎮 <b>{info['name']}</b>\n"
            f"💰 Цена: {info['price']}\n"
            f"🏷 Жанры: {info['genres']}\n"
            f"📝 {info['description']}\n"
            f"🔗 <a href='https://store.steampowered.com/app/{appid}/'>Ссылка</a>\n\n"
            f"<i>Таблица обновлена. Всего игр: {len(games)}</i>\n"
            f"Показать список: /table"
        )
        await update.message.reply_text(reply, parse_mode="HTML", disable_web_page_preview=True)

async def table_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    table = format_table()
    await update.message.reply_text(table, parse_mode="HTML")

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет файл games.json для резервного копирования"""
    if not os.path.exists(DATA_FILE):
        await update.message.reply_text("Нет данных для резервного копирования.")
        return
    with open(DATA_FILE, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="games_backup.json",
            caption="Резервная копия таблицы. При необходимости отправь этот файл боту, и он восстановит данные."
        )

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Восстановление из присланного файла (ожидает документ)"""
    await update.message.reply_text("Отправь мне файл games_backup.json, и я восстановлю таблицу.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает полученный документ для восстановления"""
    doc = update.message.document
    if not doc.file_name.endswith(".json"):
        return
    file = await doc.get_file()
    content = await file.download_as_bytearray()
    try:
        data = json.loads(content.decode("utf-8"))
        if isinstance(data, dict):
            global games
            games = data
            save_games(games)
            await update.message.reply_text(f"✅ Таблица восстановлена! Игр: {len(games)}")
        else:
            await update.message.reply_text("❌ Неверный формат файла.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка восстановления: {e}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(CommandHandler("table", table_command))
    app.add_handler(CommandHandler("t", table_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), handle_document))

    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8443))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{webhook_url}/{TOKEN}")
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
