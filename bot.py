import re, logging, requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TOKEN"]

# Хранилище игр в памяти: {appid: {данные}}
games = {}

def get_steam_data(appid):
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=ru&l=russian"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data or str(appid) not in data or not data[str(appid)]["success"]:
            return None
        g = data[str(appid)]["data"]
        name = g.get("name", "Без названия")

        price_info = g.get("price_overview")
        if price_info:
            price = f"{price_info['final']/100:.2f} ₽"
        else:
            price = "Бесплатно" if g.get("is_free") else "Нет цены"

        genres_list = [x["description"] for x in g.get("genres", [])]
        genres = ", ".join(genres_list) if genres_list else "Не указаны"

        description = g.get("short_description", "—")

        return {
            "name": name,
            "price": price,
            "genres": genres,
            "description": description
        }
    except Exception as e:
        logging.error(f"Steam error: {e}")
        return None

def format_table():
    if not games:
        return "Таблица пока пуста. Киньте ссылку на игру Steam."

    sorted_games = sorted(games.items(), key=lambda x: x[1].get("Дата обновления", ""), reverse=True)
    table = "<b>📊 Сравнительная таблица игр</b>\n\n<pre>"
    table += f"{'Название':<15} {'Цена':<8} {'Жанры':<18} {'Описание':<45}\n"
    table += "-" * 86 + "\n"

    for appid, row in sorted_games:
        name = row.get("Название", "?")[:14]
        price = row.get("Цена (RUB)", "?")[:7]
        genres = row.get("Жанры", "?")[:17]
        desc = row.get("Описание", "—")[:44]
        table += f"{name:<15} {price:<8} {genres:<18} {desc:<45}\n"

    table += "</pre>"
    table += f"\nВсего игр: <b>{len(games)}</b>"
    return table

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    pattern = r"https?://store\.steampowered\.com/app/(\d+)"
    appids = re.findall(pattern, text)
    if not appids:
        return

    for appid in set(appids):
        info = get_steam_data(appid)
        if not info:
            await update.message.reply_text(f"❌ Не удалось получить данные для {appid}")
            continue

        row = {
            "Дата обновления": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Название": info["name"],
            "Цена (RUB)": info["price"],
            "Жанры": info["genres"],
            "Описание": info["description"],
            "Ссылка": f"https://store.steampowered.com/app/{appid}/"
        }

        games[appid] = row

        reply = (
            f"🎮 <b>{info['name']}</b>\n"
            f"💰 Цена: {info['price']}\n"
            f"🏷 Жанры: {info['genres']}\n"
            f"📝 Описание: {info['description']}\n"
            f"🔗 <a href='https://store.steampowered.com/app/{appid}/'>Ссылка</a>\n\n"
            f"<i>Таблица обновлена. Всего игр: {len(games)}</i>\n"
            f"Показать таблицу: /table"
        )
        await update.message.reply_text(reply, parse_mode="HTML", disable_web_page_preview=True)

async def table_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    table = format_table()
    await update.message.reply_text(table, parse_mode="HTML")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(CommandHandler("table", table_command))
    app.add_handler(CommandHandler("t", table_command))

    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8443))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{webhook_url}/{TOKEN}")
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
