import re, os, json, logging, requests
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TOKEN"]
DATA_FILE = "games.json"
PIN_MSG_KEY = "pinned_chat_id"

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

# Текст таблицы для команд и закрепа
def build_pin_text():
    game_items = {k: v for k, v in games.items() if k != PIN_MSG_KEY}
    if not game_items:
        return "📊 Таблица пока пуста. Добавьте ссылку на игру Steam."

    sorted_games = sorted(game_items.items(), key=lambda x: x[1].get("Дата обновления", ""), reverse=True)
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

    header = f"<b>📊 Игры в списке ({len(game_items)} шт.)</b>\n"
    separator = "\n" + "—" * 25 + "\n"
    footer = "\n\nОбновить: /full | Кратко: /short"
    return header + separator.join(blocks) + footer

async def update_pin(context: ContextTypes.DEFAULT_TYPE):
    """Создаёт или обновляет закреплённое сообщение (только по команде /pin)."""
    chat_id = games.get(PIN_MSG_KEY, {}).get("chat_id")
    msg_id = games.get(PIN_MSG_KEY, {}).get("msg_id")
    text = build_pin_text()
    if chat_id and msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return
        except Exception as e:
            logging.warning(f"Не удалось отредактировать закреп: {e}")
    if chat_id:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            await msg.pin()
            games[PIN_MSG_KEY] = {"chat_id": chat_id, "msg_id": msg.message_id}
            save_games(games)
        except Exception as e:
            logging.error(f"Ошибка создания закрепа: {e}")

async def restore_from_pin(app: Application):
    """Восстанавливает игры из закреплённого сообщения при старте."""
    global games
    chat_id = games.get(PIN_MSG_KEY, {}).get("chat_id")
    msg_id = games.get(PIN_MSG_KEY, {}).get("msg_id")
    if not chat_id or not msg_id:
        return
    try:
        msg = await app.bot.get_message(chat_id=chat_id, message_id=msg_id)
        text = msg.text or msg.caption
        if not text:
            return
        pattern = r'href="https?://store\.steampowered\.com/app/(\d+)/"'
        new_games = {}
        for match in re.finditer(pattern, text):
            appid = match.group(1)
            new_games[appid] = {
                "Дата обновления": "восстановлено",
                "Название": "? (обновите ссылку)",
                "Цена": "?",
                "Жанры": "?",
                "Описание": "?",
                "Ссылка": f"https://store.steampowered.com/app/{appid}/"
            }
        if new_games:
            games.update(new_games)
            save_games(games)
            logging.info(f"Восстановлено {len(new_games)} игр из закрепа")
    except Exception as e:
        logging.warning(f"Не удалось восстановить из закрепа: {e}")

# ==== Обработчики команд ====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    chat_id = update.effective_chat.id
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
        # Сохраняем id чата для будущего закрепа (но сам закреп не трогаем)
        if PIN_MSG_KEY not in games:
            games[PIN_MSG_KEY] = {"chat_id": chat_id}
        else:
            games[PIN_MSG_KEY]["chat_id"] = chat_id
        save_games(games)

    game_count = len([k for k in games if k != PIN_MSG_KEY])
    await update.message.reply_text(
        f"✅ Игра добавлена. Всего игр: {game_count}\n"
        f"Показать список: /full или /short\n"
        f"Закрепить таблицу: /pin"
    )

async def full_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = build_pin_text()
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def short_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game_items = {k: v for k, v in games.items() if k != PIN_MSG_KEY}
    if not game_items:
        await update.message.reply_text("Таблица пока пуста.")
        return
    sorted_games = sorted(game_items.items(), key=lambda x: x[1].get("Дата обновления", ""), reverse=True)
    lines = ["<b>📋 Краткий список</b>\n"]
    for appid, row in sorted_games:
        name = row.get("Название", "?")
        price = row.get("Цена", "?")
        link = row.get("Ссылка", f"https://store.steampowered.com/app/{appid}/")
        lines.append(f'• <a href="{link}">{name}</a> — {price}')
    lines.append(f"\nВсего игр: {len(game_items)}")
    lines.append("Полная версия: /full")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительно обновляет закреплённое сообщение."""
    await update_pin(context)
    await update.message.reply_text("✅ Таблица закреплена / обновлена.")

async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(DATA_FILE):
        await update.message.reply_text("Нет данных.")
        return
    with open(DATA_FILE, "rb") as f:
        await update.message.reply_document(document=f, filename="games_backup.json", caption="Резервная копия")

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
            save_games(games)
            cnt = len([k for k in games if k != PIN_MSG_KEY])
            await update.message.reply_text(f"✅ Таблица восстановлена! Игр: {cnt}")
        else:
            await update.message.reply_text("❌ Неверный формат.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

def main():
    app = Application.builder().token(TOKEN).post_init(restore_from_pin).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    # Основные команды
    app.add_handler(CommandHandler("full", full_table))
    app.add_handler(CommandHandler("f", full_table))
    app.add_handler(CommandHandler("short", short_table))
    app.add_handler(CommandHandler("s", short_table))
    app.add_handler(CommandHandler("table", full_table))
    app.add_handler(CommandHandler("t", full_table))
    # Управление закрепом
    app.add_handler(CommandHandler("pin", pin_command))
    # Бэкап/восстановление
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
