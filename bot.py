def get_steam_data(appid):
    # Пробуем русский регион
    for region, lang, currency_label in [
        ("ru", "russian", "RUB"),
        ("us", "english", "USD")   # запасной вариант
    ]:
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc={region}&l={lang}"
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            if not data or str(appid) not in data or not data[str(appid)]["success"]:
                continue  # не вышло — пробуем следующий регион

            g = data[str(appid)]["data"]
            name = g.get("name", "Без названия")

            price_info = g.get("price_overview")
            if price_info:
                price = f"{price_info['final']/100:.2f} {currency_label}"
            else:
                price = "Бесплатно" if g.get("is_free") else "Нет цены"

            genres_list = [x["description"] for x in g.get("genres", [])]
            genres = ", ".join(genres_list) if genres_list else "Не указаны"

            description = g.get("short_description", "—")

            return {
                "name": name,
                "price": price,
                "genres": genres,
                "description": description,
                "currency": currency_label
            }

        except Exception as e:
            logging.error(f"Steam error for {appid} (cc={region}): {e}")
            continue

    # Если оба региона не дали данных
    return None
