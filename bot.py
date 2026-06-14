import re, csv, os, logging, requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TOKEN"]
CSV_FILE = "steam_games.csv"

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["Дата", "Название", "Цена (RUB)", "Жанры", "Ссылка"])

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

        # Цена в рублях
        price_info = g.get("price_overview")
        if price_info:
            rubles = price_info["final"] / 100
            price = f"{rubles:.2f} ₽"
        else:
            price = "Бесплатно" if g.get("is_free") else "Нет цены"

        # Жанры
        genres_list = [x["description"] for x in g.get("genres", [])]
        genres = ", ".join(genres_list) if genres_list else "Не указаны"

        return {"name": name, "price": price, "genres": genres}

    except Exception as e:
        logging.error(f"Ошибка Steam API: {e}")
        return None

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

        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            info["name"],
            info["price"],
            info["genres"],
            f"https://store.steampowered.com/app/{appid}/"
        ]
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

        reply = (
            f"🎮 <b>{info['name']}</b>\n"
            f"💰 Цена: {info['price']}\n"
            f"🏷 Жанры: {info['genres']}\n"
            f"🔗 <a href='https://store.steampowered.com/app/{appid}/'>Ссылка</a>"
        )
        await update.message.reply_text(reply, parse_mode="HTML", disable_web_page_preview=True)

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0:
        await update.message.reply_text("Таблица пуста")
        return
    with open(CSV_FILE, "rb") as f:
        await update.message.reply_document(document=f, filename="steam_games.csv", caption="Таблица")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(CommandHandler("export", export))
    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8443))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{webhook_url}/{TOKEN}")
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
