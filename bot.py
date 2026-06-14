import re, csv, os, logging, requests, json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TOKEN"]
CSV_FILE = "steam_games.csv"

if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["Дата", "Название", "Цена (RUB)", "Жанры", "Отзывы", "Ссылка"])

def get_steam_data(appid):
    """Получает данные об игре с русским языком и ценами в рублях"""
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=ru&l=russian"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data or str(appid) not in data or not data[str(appid)]["success"]:
            return None
        g = data[str(appid)]["data"]

        # Для отладки выведем в логи ВСЕ поля игры (можно будет посмотреть на Render)
        logging.info(f"Steam data for appid {appid}: {json.dumps(g, ensure_ascii=False, indent=2)}")

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

        # Отзывы: пробуем несколько вариантов
        review_desc = g.get("review_score_desc", "")  # Например "Очень положительные"
        if review_desc:
            review_desc = review_desc.strip()

        # Количество обзоров
        total_reviews = g.get("total_reviews")  # Современное поле
        if not total_reviews:
            # Запасной вариант через recommendations
            rec = g.get("recommendations", {})
            if rec:
                total_reviews = rec.get("total", 0)

        # Собираем строку рейтинга
        if total_reviews and review_desc:
            player_rating = f"{review_desc} ({total_reviews} обз.)"
        elif review_desc:
            player_rating = review_desc
        elif total_reviews:
            # Есть количество, но нет описания (маловероятно)
            player_rating = f"Отзывы: {total_reviews}"
        else:
            player_rating = "Нет оценок"

        return {
            "name": name,
            "price": price,
            "genres": genres,
            "player_rating": player_rating
        }

    except Exception as e:
        logging.error(f"Ошибка Steam API для appid {appid}: {e}")
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
            await update.message.reply_text(f"❌ Не удалось получить данные для приложения {appid}")
            continue

        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            info["name"],
            info["price"],
            info["genres"],
            info["player_rating"],
            f"https://store.steampowered.com/app/{appid}/"
        ]
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

        reply = (
            f"🎮 <b>{info['name']}</b>\n"
            f"💰 Цена: {info['price']}\n"
            f"🏷 Жанры: {info['genres']}\n"
            f"👍 Отзывы: {info['player_rating']}\n"
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
