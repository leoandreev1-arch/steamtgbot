def save_all_data() -> None:
    global storage_msg_id
    # Компактный JSON: без пробелов после ',' и ':'
    text = json.dumps(all_games, ensure_ascii=False, separators=(',', ':'))
    try:
        if storage_msg_id:
            # Пробуем редактировать известное сообщение
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
            # Если не получилось – сбросим ID и создадим новое
            storage_msg_id = None

        # Создаём новое сообщение и запоминаем его ID
        resp = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": STORAGE_CHAT_ID, "text": text},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            storage_msg_id = data["result"]["message_id"]
    except Exception as exc:
        logger.error("Ошибка сохранения в хранилище: %s", exc)
