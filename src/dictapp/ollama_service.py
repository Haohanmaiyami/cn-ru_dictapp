import json
import httpx
import time
import re
from dictapp.models import Entry
from dictapp.settings import settings
from pypinyin import Style, lazy_pinyin

AI_CACHE = {}
CACHE_TTL = 60 * 60

def clean_chinese(text: str) -> str:
    return "".join(re.findall(r'[\u4e00-\u9fff]+', text))

def build_dictionary_context(entries: list[Entry]) -> str:
    if not entries:
        return "Словарные данные не найдены."

    lines: list[str] = []

    for i, entry in enumerate(entries, start=1):
        pos_part = f" [{entry.pos}]" if entry.pos else ""
        pinyin_part = f" — {entry.pinyin}" if entry.pinyin else ""
        ru_part = (entry.ru or "").strip().replace("\n", " ")
# убираем лишние пробелы
        ru_part = " ".join(ru_part.split())

# берём только первое значение (до ;)
        ru_part = ru_part.split("；")[0]

# жёстко режем длину
        ru_part = ru_part[:50]
        lines.append(f"{i}. {entry.hanzi}{pinyin_part}{pos_part} — {ru_part}")

    return "\n".join(lines)


def build_analysis_prompt(text: str, dictionary_context: str = "") -> str:
    return f"""
Ты переводчик с китайского на русский.

Верни только валидный JSON.

Формат:
{{
  "literal": "",
  "natural": "",
  "keywords": []
}}

Правила:
- literal и natural только на русском
- НЕ используй английский
- НЕ вставляй китайский в перевод
- НЕ добавляй объяснения
- keywords = китайские слова из текста
- Если предложение вопросительное, natural должен быть нормальным русским вопросом
- НЕ делай дословный кривой перевод, делай естественный русский
- Для сложных предложений natural должен звучать как обычный русский
- keywords не должны быть местоимениями и служебными словами: 我, 你, 这个, 那个, 是, 的, 了, 太, 不
- keywords должны быть цельными смысловыми выражениями

Словарные подсказки:
{dictionary_context}

Пример:

Китайский текст:
我昨天给你买的那个东西你还记得吗？

Ответ:
{{
  "literal": "Ты ещё помнишь ту вещь, которую я вчера купил тебе?",
  "natural": "Ты помнишь то, что я купил тебе вчера?",
  "keywords": ["昨天", "买东西", "还记得"]
}}

Китайский текст:
{text}
""".strip()

def fallback_analysis(text: str, dictionary_entries: list[Entry]) -> dict:
    keywords: list[str] = []

    bad_keywords = {
        "我", "你", "他", "她", "它", "们",
        "这个", "那个", "这些", "那些",
        "是", "了", "的", "得", "地",
        "太", "很", "不", "吗", "呢", "啊",
        "个是", "的是", "的是太", "是太",
        "于下", "心开",
    }

    for entry in dictionary_entries:
        hanzi = (entry.hanzi or "").strip()
        ru = (entry.ru or "").strip()

        if not hanzi:
            continue

        if hanzi in bad_keywords:
            continue

        if len(hanzi) < 2:
            continue

        if ru == "_" or ru.startswith("_"):
            continue

        if hanzi not in keywords:
            keywords.append(hanzi)

    return {
        "literal": "",
        "natural": "",
        "pinyin": " ".join(lazy_pinyin(text, style=Style.TONE)),
        "keywords": keywords[:8],
    }

async def analyze_with_ollama(text: str, dictionary_entries: list[Entry]) -> dict:
    cache_key = ("cn_ru", text.strip())

    cached = AI_CACHE.get(cache_key)

    if cached:
        result, timestamp = cached

        if time.time() - timestamp < CACHE_TTL:
            print("⚡ CN_RU CACHE HIT")
            return result
    # 🔥 быстрый ответ БЕЗ Ollama
    simple_translations = {
        "你好": ("Привет", "Здравствуйте", ["你好"]),
        "谢谢": ("Спасибо", "Спасибо", ["谢谢"]),
        "对不起": ("Извините", "Извините", ["对不起"]),
        "再见": ("До свидания", "До свидания", ["再见"]),
    }

    if text.strip() in simple_translations:
        literal, natural, keywords = simple_translations[text.strip()]
        return {
            "literal": literal,
            "natural": natural,
            "pinyin": " ".join(lazy_pinyin(text, style=Style.TONE)),
            "keywords": keywords,
        }

    # ⬇️ только если не простой кейс — идём в Ollama
    dictionary_context = build_dictionary_context(dictionary_entries)
    prompt = build_analysis_prompt(text=text, dictionary_context=dictionary_context)

    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "num_predict": 160,
            "temperature": 0.1,
            "num_ctx": 2048
        },
    }

    timeout = httpx.Timeout(
        connect=10.0,
        read=300.0,
        write=10.0,
        pool=10.0,
    )

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{settings.ollama_base_url}/api/generate",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
            break


        except httpx.ReadTimeout:

            print(f"⏳ Timeout, retry {attempt + 1}")

            if attempt == 1:
                return fallback_analysis(text, dictionary_entries)



        except Exception as e:

            print(f"❌ Ollama error: {e}")

            return fallback_analysis(text, dictionary_entries)

    raw_response = (data.get("response") or "").strip()

    try:
        parsed = json.loads(raw_response)



    except json.JSONDecodeError:

        # >>> CHANGE: пытаемся вытащить JSON вручную

        try:

            start = raw_response.find("{")

            end = raw_response.rfind("}") + 1

            if start == -1 or end <= start:
                return fallback_analysis(text, dictionary_entries)

            cleaned = raw_response[start:end]

            parsed = json.loads(cleaned)


        except Exception:

            return fallback_analysis(text, dictionary_entries)

    literal = str(parsed.get("literal", "") or "").strip()
    natural = str(parsed.get("natural", "") or "").strip()
    pinyin = " ".join(lazy_pinyin(text, style=Style.TONE))
    keywords = parsed.get("keywords", []) or []

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

    BAD_KEYWORDS = {
        "我", "你", "他", "她", "它", "们",
        "这个", "那个", "这些", "那些",
        "是", "了", "的", "得", "地",
        "太", "很", "不", "吗", "呢", "啊",
    }

    clean_keywords = []

    for word in keywords:
        word = str(word).strip()

        if not word:
            continue

        if word in BAD_KEYWORDS:
            continue

        if len(word) < 2:
            continue

        if not any("\u3400" <= ch <= "\u9fff" for ch in word):
            continue

        clean_keywords.append(word)

    for entry in dictionary_entries:
        hanzi = (entry.hanzi or "").strip()
        ru = (entry.ru or "").strip()

        if not hanzi:
            continue

        if hanzi in BAD_KEYWORDS:
            continue

        if len(hanzi) < 2:
            continue

        if ru == "_" or ru.startswith("_"):
            continue

        if hanzi in {"个是", "的是", "的是太", "这个", "是太"}:
            continue

        if hanzi not in clean_keywords:
            clean_keywords.append(hanzi)

    keywords = clean_keywords[:8]

    if not keywords:
        keywords = [
            (entry.hanzi or "").strip()
            for entry in dictionary_entries
            if (entry.hanzi or "").strip()
        ][:8]

    result = {
        "literal": literal,
        "natural": natural,
        "pinyin": pinyin,
        "keywords": keywords,
    }

    AI_CACHE[cache_key] = (result, time.time())

    return result


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
Ты переводчик с русского на китайский.

Верни только JSON:

{{
  "translation": "",
  "comment": ""
}}

Правила:
- translation: только упрощённый китайский
- НЕ используй английский
- comment: коротко на русском
- не добавляй лишний смысл

Русский текст:
{text}
""".strip()

async def translate_ru_to_cn_with_ollama(text: str) -> dict:
    prompt = build_ru_to_cn_prompt(text=text)

    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "num_predict": 80,
            "temperature": 0,
            "top_p": 0.8,
            "repeat_penalty": 1.1,
            "num_ctx": 1024
        },
    }

    timeout = httpx.Timeout(
        connect=10.0,
        read=300.0,
        write=10.0,
        pool=10.0,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

    except httpx.ReadTimeout:
        return {
            "translation": "",
            "pinyin": "",
            "comment": "",
        }

    except Exception as e:
        print(f"❌ Ollama error: {e}")
        return {
            "translation": "",
            "pinyin": "",
            "comment": "",
        }

    raw_response = (data.get("response") or "").strip()

    try:
        parsed = json.loads(raw_response)

    except json.JSONDecodeError:
        cn_translation = clean_chinese(raw_response)
        generated_pinyin = generate_pinyin(cn_translation)

        return {
            "translation": cn_translation,
            "pinyin": generated_pinyin,
            "comment": "",
        }

    cn_translation = str(parsed.get("translation", "") or "").strip()
    comment = str(parsed.get("comment", "") or "").strip()

    # fallback если модель вернула пусто или мусор
    if not cn_translation:
        cn_translation = raw_response.strip()

    # оставляем только китайские иероглифы
    cn_translation = clean_chinese(cn_translation)

    generated_pinyin = generate_pinyin(cn_translation)

    return {
        "translation": cn_translation,
        "pinyin": generated_pinyin,
        "comment": comment,
    }

async def warmup_ollama():
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": "Переведи на русский строго JSON: 你好",
                    "stream": False,
                    "keep_alive": "-1",
                    "options": {
                        "num_predict": 30,
                        "temperature": 0.1,
                        "num_ctx": 1024
                    },
                },
            )

            await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": "Переведи на китайский строго JSON: Я хочу пить воду",
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {
                        "num_predict": 30,
                        "temperature": 0.1,
                        "num_ctx": 1024
                    },
                },
            )

        print("🔥 Ollama warmed up")
    except Exception as e:
        print(f"❌ Warmup failed: {e}")

