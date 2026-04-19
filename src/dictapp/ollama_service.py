import json

import httpx
from dictapp.models import Entry
from dictapp.settings import settings


def build_dictionary_context(entries: list[Entry]) -> str:
    if not entries:
        return "Словарные данные не найдены."

    lines: list[str] = []

    for i, entry in enumerate(entries, start=1):
        pos_part = f" [{entry.pos}]" if entry.pos else ""
        pinyin_part = f" — {entry.pinyin}" if entry.pinyin else ""

        ru_part = (entry.ru or "").strip().replace("\n", " ")
        ru_part = " ".join(ru_part.split())

        if len(ru_part) > 180:
            ru_part = ru_part[:180] + "..."

        lines.append(f"{i}. {entry.hanzi}{pinyin_part}{pos_part} — {ru_part}")

    return "\n".join(lines)


def build_analysis_prompt(text: str, dictionary_context: str) -> str:
    return f"""
Ты помощник по китайскому языку для русскоязычного пользователя.

Задача:
проанализировать китайское предложение и вернуть СТРОГО валидный JSON.

ВАЖНО:
- literal и natural должны быть ТОЛЬКО на русском языке
- НЕЛЬЗЯ оставлять китайский текст в literal или natural
- Если не уверен в literal, всё равно дай максимально близкий русский перевод
- Если не уверен в natural, всё равно дай естественный русский перевод
- pinyin должен быть для ВСЕЙ китайской фразы целиком
- keywords должны быть китайскими словами или выражениями из исходной фразы
- Никакого текста вне JSON
- Никаких пояснений
- Никаких markdown-блоков
- Никаких символов ```json
- Нельзя писать многоточия "..."
- Нельзя дублировать исходное китайское предложение в полях literal и natural
- Если не можешь надёжно дать pinyin, верни пустую строку ""
- Если не можешь надёжно дать keywords, верни пустой список []

Верни ответ СТРОГО в таком формате:

{{
  "literal": "русский буквальный перевод",
  "natural": "русский естественный перевод",
  "pinyin": "полный пиньинь всей фразы с тонами или пустая строка",
  "keywords": ["китайское слово 1", "китайское слово 2", "китайское слово 3"]
}}

Проверь перед ответом:
1. literal написан по-русски
2. natural написан по-русски
3. literal не совпадает с исходным китайским текстом
4. natural не совпадает с исходным китайским текстом
5. JSON валиден

Исходное предложение:
{text}

Словарные данные:
{dictionary_context}
""".strip()


# >>> CHANGE: теперь функция возвращает dict, а не строку
async def analyze_with_ollama(text: str, dictionary_entries: list[Entry]) -> dict:
    dictionary_context = build_dictionary_context(dictionary_entries)
    prompt = build_analysis_prompt(text=text, dictionary_context=dictionary_context)

    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            f"{settings.ollama_base_url}/api/generate",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    # >>> CHANGE: раньше возвращали просто текст
    raw_response = (data.get("response") or "").strip()

    # >>> CHANGE: пытаемся распарсить JSON
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return {
            "literal": "",
            "natural": raw_response,
            "pinyin": "",
            "keywords": [],
        }

    literal = str(parsed.get("literal", "") or "").strip()
    natural = str(parsed.get("natural", "") or "").strip()
    pinyin = str(parsed.get("pinyin", "") or "").strip()
    keywords = parsed.get("keywords", []) or []

    if any("\u3400" <= ch <= "\u9fff" for ch in literal):
        literal = ""

    if any("\u3400" <= ch <= "\u9fff" for ch in natural):
        natural = ""

    return {
        "literal": literal,
        "natural": natural,
        "pinyin": pinyin,
        "keywords": keywords,
    }



def build_ru_to_cn_prompt(text: str) -> str:
    return f"""
Ты помощник по китайскому языку для русскоязычного пользователя.

Переведи русское предложение на китайский язык.
Отвечай только на русском и китайском по шаблону ниже.
Не пиши длинних объяснений.
Сделай перевод естественным и разговорным, если контекст нейтральный.
Если возможны 2 варианта, дай самый естественный один основной вариант.

Верни ответ СТРОГО в таком формате:

Китайский перевод:
...

Пиньинь:
...

Краткий комментарий:
...

Русское предложение:
{text}
""".strip()


async def translate_ru_to_cn_with_ollama(text: str) -> str:
    prompt = build_ru_to_cn_prompt(text=text)

    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            f"{settings.ollama_base_url}/api/generate",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    return (data.get("response") or "").strip()