import json

import httpx
from dictapp.models import Entry
from dictapp.settings import settings
from pypinyin import Style, lazy_pinyin


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
Ты китайско-русский переводчик для русскоязычного пользователя.

ЗАДАЧА:
Переведи ТОЛЬКО исходное китайское предложение пользователя на русский язык.

ВАЖНО:
Словарные данные используй только как справку.
НЕ копируй из словаря длинные примеры, имена, лишние фразы и старые статьи.
НЕ добавляй ничего, чего нет в исходном китайском предложении.

Верни СТРОГО валидный JSON без markdown и без текста вокруг:

{{
  "literal": "буквальный перевод на русском",
  "natural": "естественный перевод на русском",
  "keywords": ["китайское слово 1", "китайское слово 2", "китайское выражение"]
}}

СТРОГИЕ ПРАВИЛА:
- literal и natural должны быть ТОЛЬКО на русском языке
- НЕ вставляй китайский текст в literal или natural
- НЕ смешивай русский и китайский в одном поле
- НЕ возвращай исходное китайское предложение как перевод
- literal = более близкий, буквальный русский смысл
- natural = нормальный естественный русский перевод
- keywords бери ТОЛЬКО из исходного китайского предложения
- keywords должны быть китайскими словами или выражениями
- НЕ бери keywords из словарных примеров
- Если предложение сложное, всё равно дай нормальный русский перевод
- Если не уверен, переведи максимально естественно по смыслу
- Не пиши пояснения вне JSON
- НЕ добавляй имена, обращения или персонажей, если их нет в исходном тексте
- НЕ добавляй слова вроде "госпожа", "господин", "мистер", если их нет во входе
- Перевод должен строго соответствовать исходному предложению
- Если исходное предложение короткое (1–3 слова), НЕ додумывай контекст
- Переводи максимально буквально и просто

ПРИМЕР:
Исходное предложение:
我昨天给你买的那个东西你还记得吗？

Правильный ответ:
{{
  "literal": "Ты ещё помнишь ту вещь, которую я вчера купил тебе?",
  "natural": "Ты ещё помнишь ту вещь, которую я купил тебе вчера?",
  "keywords": ["昨天", "给你买", "那个东西", "还记得"]
}}

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
    pinyin = " ".join(lazy_pinyin(text, style=Style.TONE))
    keywords = parsed.get("keywords", []) or []

    simple_translations = {
        "你好": ("Привет", "Здравствуйте", ["你好"]),
        "谢谢": ("Спасибо", "Спасибо", ["谢谢"]),
        "对不起": ("Извините", "Извините", ["对不起"]),
        "再见": ("До свидания", "До свидания", ["再见"]),
    }

    if text.strip() in simple_translations:
        literal, natural, keywords = simple_translations[text.strip()]

    def has_chinese(value: str) -> bool:
        return any("\u3400" <= ch <= "\u9fff" for ch in value)

    def has_russian(value: str) -> bool:
        return any("а" <= ch.lower() <= "я" or ch == "ё" for ch in value)

    if has_chinese(literal) and not has_russian(literal):
        literal = ""

    if has_chinese(natural) and not has_russian(natural):
        natural = ""

    if not natural and literal:
        natural = literal

    if not literal and natural:
        literal = natural

    return {
        "literal": literal,
        "natural": natural,
        "pinyin": pinyin,
        "keywords": keywords,
    }

def generate_pinyin(text: str) -> str:
    if not text:
        return ""

    return " ".join(lazy_pinyin(text, style=Style.TONE))

def extract_cn_translation(raw: str) -> str:
    lines = raw.splitlines()

    for index, line in enumerate(lines):
        clean = line.strip()

        if clean.startswith("Китайский перевод:") or clean.startswith("Китайский перевод："):
            value = clean.replace("Китайский перевод:", "").replace("Китайский перевод：", "").strip()

            if value:
                return value

            if index + 1 < len(lines):
                return lines[index + 1].strip()

    return ""

def build_ru_to_cn_prompt(text: str) -> str:
    return f"""
Ты помощник по китайскому языку для русскоязычного пользователя.

ЗАДАЧА:
Переведи русское предложение на китайский язык.

СТРОГИЕ ПРАВИЛА:
- Используй ТОЛЬКО упрощённые китайские иероглифы (Simplified Chinese)
- НЕ используй традиционные иероглифы
- Пиньинь обязательно с тонами (например: wǒ lèi le)
- Перевод должен быть естественным и разговорным
- НЕ добавляй ничего вне шаблона
- НЕ добавляй пояснения вне блока "Краткий комментарий"
- Если есть несколько вариантов — выбери ОДИН самый естественный
- Строка "Пиньинь:" обязательна. Никогда не пропускай её.
- Даже если фраза короткая, обязательно дай пиньинь для всего китайского перевода.

ФОРМАТ ОТВЕТА (строго соблюдай):

Китайский перевод:
...

Пиньинь:
...

Краткий комментарий:
...

Русское предложение:
{text}
""".strip()


async def translate_ru_to_cn_with_ollama(text: str) -> dict:
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

    raw = (data.get("response") or "").strip()

    cn_translation = extract_cn_translation(raw)
    generated_pinyin = generate_pinyin(cn_translation)

    return {
        "translation": raw,
        "pinyin": generated_pinyin,
    }