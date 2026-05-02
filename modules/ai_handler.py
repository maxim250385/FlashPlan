import os
import aiohttp
import json
import base64
import random
import logging
import zoneinfo
from datetime import datetime

MSK_TZ = zoneinfo.ZoneInfo("Europe/Moscow")

MODELS_PRIORITY = [
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash"
]

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


def _load_proxies() -> list[str]:
    raw = os.environ.get("PROXY_HOSTS", "")
    result = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        # host:port:login:password
        if len(parts) == 4:
            host, port, login, password = parts
            result.append(f"http://{login}:{password}@{host}:{port}")
        # host:port (без авторизации)
        elif len(parts) == 2:
            result.append(f"http://{entry}")
        # уже полный URL
        elif entry.startswith("http"):
            result.append(entry)
    return result


ALL_PROXIES: list[str] = _load_proxies()

_GEMINI_KEYS: list[str] = [
    k for k in [os.environ.get("GEMINI_API_KEY_1"), os.environ.get("GEMINI_API_KEY_2")]
    if k
]


async def _call_gemini(contents: list) -> dict | None:
    payload = {
        "contents": [{"parts": contents}],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    api_key = random.choice(_GEMINI_KEYS)
    proxy_url = random.choice(ALL_PROXIES) if ALL_PROXIES else None

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        for model in MODELS_PRIORITY:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                async with session.post(url, json=payload, proxy=proxy_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        try:
                            result_text = data['candidates'][0]['content']['parts'][0]['text']
                            return json.loads(result_text)
                        except (KeyError, json.JSONDecodeError) as e:
                            logger.error(f"Ошибка парсинга JSON у модели {model}: {e}")
                            continue
                    else:
                        error_text = await resp.text()
                        logger.warning(f"Ошибка API ({resp.status}) у {model}: {error_text}")
                        continue
            except Exception as e:
                logger.error(f"Ошибка сети у {model} через {proxy_url}: {e}")
                continue

    logger.critical("Все модели из MODELS_PRIORITY недоступны!")
    return None


async def analyze_intent(text: str = None, audio_bytes: bytes = None) -> dict | None:
    now_msk = datetime.now(MSK_TZ)

    prompt = (
        f"Текущая дата и время: {now_msk.strftime('%Y-%m-%d %H:%M')}. Часовой пояс: Москва.\n"
        "Ты — диспетчер личного ассистента. Классифицируй запрос и верни СТРОГО JSON без markdown.\n\n"
        "ПРАВИЛА (intent):\n"
        "1. calendar_event — конкретное событие с датой (встреча, запись, поездка, задача) БЕЗ слова «напомни».\n"
        "   Примеры: «купить жене цветы в 11:00», «встреча 15 мая», «завтра в 9 зубной».\n"
        "   Если имя названо в самом начале (например «Максим, запись к врачу в 10»), "
        "   это владелец — запиши в person, не включай в event.\n"
        "2. reminder — запрос со словом «напомни» / «напомните» + любое время:\n"
        "   - Относительное: «через 3 часа», «через 20 минут»\n"
        "   - Завтра/послезавтра/день недели без часа: «напомни завтра», «напомни в пятницу» — "
        "ставь время 09:00 если час не указан\n"
        "   - Завтра/дата с часом: «напомни завтра в 15:00 про врача»\n"
        "   Рассчитай точное trigger_at (YYYY-MM-DD HH:MM:SS) от текущего времени.\n"
        "3. note_add — запомнить информацию без времени: фио, номер, пароль, характеристика, факт.\n"
        "4. note_edit — исправить существующую заметку: «не такое а такое», «измени», «исправь».\n"
        "5. note_query — найти сохранённую информацию: «напомни фио», «какое масло», «что записано».\n"
        "   category — широкое ключевое слово темы (не обязательно точное совпадение с названием категории).\n"
        "   Примеры: «что там за масло» → category=«масло», «кто доктор» → category=«доктор».\n"
        "6. note_delete — удалить заметку: «удали запись про ...», «забудь».\n"
        "7. checklist_add — добавить позиции в именованный список покупок или дел.\n"
        "   Примеры: «нужно купить молоко хлеб», «добавь колбасу в покупки».\n"
        "   list_name — ТОЧНОЕ слово из таблицы ниже или «прочее» если ничего не подходит.\n"
        "   items — массив строк с отдельными позициями.\n"
        "8. checklist_query — показать список: «что купить», «покажи список покупки», «что в аптеке».\n"
        "   list_name — название САМОГО СПИСКА (не магазин, не место, не бренд!).\n"
        "   Если пользователь упоминает магазин/место (чижик, пятёрочка, магнит) но не называет список —\n"
        "   верни list_name=«покупки». Примеры: «что в чижике» → «покупки», «что в аптеке» → «аптека».\n"
        "9. checklist_delete — удалить список целиком (item_delete=null) "
        "или один элемент (item_delete=«элемент»).\n\n"
        "ФИКСИРОВАННЫЕ list_name — использовать ТОЧНО эти слова:\n"
        "• покупки — продукты питания, еда, напитки, бакалея\n"
        "• аптека — лекарства, витамины, медицинские товары\n"
        "• авто — всё для машины: запчасти, масло, шины, колёса, автохимия\n"
        "• дом — товары для дома, мебель, бытовая техника, посуда\n"
        "• одежда — одежда, обувь, аксессуары, текстиль\n"
        "• электроника — гаджеты, телефоны, компьютеры, зарядки, кабели\n"
        "• дети — игрушки, школьные товары, детская одежда, питание для детей\n"
        "• животные — корм, ветаптека, аксессуары для питомцев\n"
        "• спорт — спортивный инвентарь, форма, снаряжение\n"
        "• дача — семена, удобрения, садовый инструмент, растения\n"
        "• работа — рабочие задачи, канцелярия, покупки для офиса\n"
        "• подарки — подарки, праздничные товары, упаковка\n"
        "• красота — косметика, парфюм, средства ухода за собой, гигиена\n"
        "• книги — книги, журналы, учебники, курсы, образование\n"
        "• путешествия — билеты, отели, вещи в поездку, турснаряжение\n"
        "• ремонт — стройматериалы, краска, плитка, сантехника, электрика\n"
        "• здоровье — врачи, медицинские процедуры, анализы (услуги, не лекарства)\n"
        "• праздник — еда, декор, развлечения для праздников и мероприятий\n"
        "• дела — бытовые поручения и задачи без конкретной категории\n"
        "• прочее — всё что явно не подходит ни к одной категории выше\n"
        "Если тема явно совпадает — использовать нужный list_name. Если неясно — «дела».\n\n"
        "Верни JSON (null для неиспользуемых полей):\n"
        '{"intent":"...","params":{'
        '"date":"YYYY-MM-DD","time":"HH:MM","event":"...","person":null,'
        '"trigger_at":"YYYY-MM-DD HH:MM:SS","text":"...",'
        '"content":"...","category":"...",'
        '"list_name":"...","items":[],"item_delete":null}}'
    )

    contents = []
    if text:
        contents.append({"text": prompt + "\nЗапрос: " + text})
    elif audio_bytes:
        contents.append({"text": prompt})
        contents.append({
            "inline_data": {
                "mime_type": "audio/ogg",
                "data": base64.b64encode(audio_bytes).decode("utf-8")
            }
        })

    return await _call_gemini(contents)
