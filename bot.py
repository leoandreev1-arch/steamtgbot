import re, os, json, logging, requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TOKEN"]
STORAGE_CHAT_ID = int(os.environ["STORAGE_CHAT_ID"])   # ID канала-хранилища
MSG_ID_KEY = "last_storage_msg_id"   # ID последнего сообщения в канале (хранится в памяти)

games = {}
# В памяти будем хранить id сообщения-хранилища, чтобы редактировать его, а не создавать новое
storage_msg_id = None

async def restore_from_storage(app):
    """При старте читает последнее сообщение из канала-хранилища и восстанавливает таблицу."""
    global games, storage_msg_id
    try:
        # Получаем последние сообщения (лимит 1)
        updates = await app.bot.get_updates(limit=1, timeout=5)
        # Альтернативно: можно попросить юзера прислать /start в канале? Но проще искать сообщение по ID.
        # Вместо этого будем использовать уже известный msg_id, если он сохранён? Но он хранится в памяти.
        # Для первой загрузки прочитаем весь канал? Лучше завести отдельную переменную в Render, чтобы хранить msg_id.
    except:
        pass
    # Поскольку msg_id может потеряться, прочитаем последнее сообщение в канале методом get_chat_history
    try:
        async for message in app.bot.get_chat_history(chat_id=STORAGE_CHAT_ID, limit=1):
            if message.text:
                data = json.loads(message.text)
                games.clear()
                games.update(data)
                storage_msg_id = message.message_id
                logging.info(f"Восстановлено {len(games)} игр из канала-хранилища")
    except Exception as e:
        logging.warning(f"Не удалось восстановить из канала: {e}")

async def save_to_storage(context):
    """Сохраняет текущую таблицу в канал-хранилище (редактирует существующее или создаёт новое)."""
    global storage_msg_id
    text = json.dumps(games, ensure_ascii=False)
    try:
        if storage_msg_id:
            await context.bot.edit_message_text(
                chat_id=STORAGE_CHAT_ID,
                message_id=storage_msg_id,
                text=text
            )
        else:
            msg = await context.bot.send_message(
                chat_id=STORAGE_CHAT_ID,
                text=text
            )
            storage_msg_id = msg.message_id
    except Exception as e:
        logging.error(f"Ошибка сохранения в канал: {e}")

def get_steam_data(appid):
    for region, lang, currency_label in [
        ("ru", "russian", "₽"),
        ("us", "russian", "USD"),
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
            is_demo = g.get("is_demo", False) or g.get("type", "") == "demo"
            name = g.get("name", "Без названия")
            price_info = g.get("price_overview")
            if is_demo:
                price = "Демо"
            elif price_info:
                amount = price_info["final"] / 100
                if currency_label == "₽":
                    price = f"{amount:.2f} ₽"
                else:
                    price = f"{amount:.2f} USD" + (" (недоступна в РФ)" if region == "us" else "")
            else:
                price = "Бесплатно" if g.get("is_free") else "Нет цены"
            genres_list = [x["description"] for x in g.get("genres", [])]
            genres = ", ".join(genres_list) if genres_list else "Не указаны"
            description = g.get("short_description", "—")
            if lang != "russian":
                description = "—"
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

def build_full_text():
    if not games:
        return "📊 Таблица пока пуста."
    sorted_games = sorted(games.items(), key=lambda x: x[1].get("Дата обновления", ""), reverse=True)
    blocks = []
    for appid, row in sorted_games:
        name = row.get("Название", "?")
        price = row.get("Цена", "?")
        genres = row.get("Жанры", "?")
        desc = row.get("Описание", "—")
        link = row.get("Ссылка", f"https://store.steampowered.com/app/{appid}/")
        block = (
            f'🎮 <b><a href="{link}">{name}</a></b>\n'
            f"💰 Цена: {price}\n"
            f"🏷 Жанры: {genres}\n"
            f"📝 <i>{desc}</i>"
        )
        blocks.append(block)
    header = f"<b>📊 Игры в списке ({len(games)} шт.)</b>\n"
    separator = "\n" + "—" * 25 + "\n"
    footer = "\n\nКратко: /short"
    return header + separator.join(blocks) + footer

def build_short_text():
    if not games:
        return "📊 Таблица пока пуста."
    sorted_games = sorted(games.items(), key=lambda x: x[1].get("Дата обновления", ""), reverse=True)
    lines = ["<b>📋 Краткий список</b>\n"]
    for appid, row in sorted_games:
        name = row.get("Название", "?")
        price = row.get("Цена", "?")
        link = row.get("Ссылка", f"https://store.steampowered.com/app/{appid}/")
        lines.append(f'• <a href="{link}">{name}</a> — {price}')
    lines.append(f"\nВсего игр: {len(games)}")
    lines.append("Полная версия: /full")
    return "\n".join(lines)

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

    await save_to_storage(context)   # <-- сохраняем в канал
    await update.message.reply_text(
        f"✅ Игра добавлена. Всего игр: {len(games)}\n"
        f"Показать: /full или /short"
    )

async def full_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_full_text(), parse_mode="HTML", disable_web_page_preview=True)

async def short_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_short_text(), parse_mode="HTML", disable_web_page_preview=True)

async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # просто отправляем JSON из памяти
    json_str = json.dumps(games, ensure_ascii=False)
    await update.message.reply_document(
        document=json_str.encode("utf-8"),
        filename="games_backup.json",
        caption="Резервная копия"
    )

async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправь мне файл games_backup.json для восстановления.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await save_to_storage(context)
            await update.message.reply_text(f"✅ Таблица восстановлена! Игр: {len(games)}")
        else:
            await update.message.reply_text("❌ Неверный формат.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

def main():
    app = Application.builder().token(TOKEN).post_init(restore_from_storage).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(CommandHandler("full", full_table))
    app.add_handler(CommandHandler("f", full_table))
    app.add_handler(CommandHandler("short", short_table))
    app.add_handler(CommandHandler("s", short_table))
    app.add_handler(CommandHandler("table", full_table))
    app.add_handler(CommandHandler("t", full_table))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("restore", restore_cmd))
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), handle_document))

    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8443))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN, webhook_url=f"{webhook_url}/{TOKEN}")
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
