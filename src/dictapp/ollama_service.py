import json
import asyncio
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
# убираем лишние пробелы
        ru_part = " ".join(ru_part.split())

# берём только первое значение (до ;)
        ru_part = ru_part.split("；")[0]

# жёстко режем длину
        ru_part = ru_part[:50]
        lines.append(f"{i}. {entry.hanzi}{pinyin_part}{pos_part} — {ru_part}")

    return "\n".join(lines)


def build_analysis_prompt(text: str) -> str:
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

Китайский текст:
{text}
""".strip()

async def analyze_with_ollama(text: str, dictionary_entries: list[Entry]) -> dict:
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
    prompt = build_analysis_prompt(text=text)

    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "num_predict": 60,
            "temperature": 0.1,
            "num_ctx": 512
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
                return {
                    "literal": "",
                    "natural": "",
                    "pinyin": "",
                    "keywords": [],
                }

        except Exception as e:
            print(f"❌ Ollama error: {e}")
            return {
                "literal": "",
                "natural": "",
                "pinyin": "",
                "keywords": [],
            }

    raw_response = (data.get("response") or "").strip()

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
        return {
            "translation": raw_response,
            "pinyin": "",
            "comment": "",
        }

    cn_translation = str(parsed.get("translation", "") or "").strip()
    comment = str(parsed.get("comment", "") or "").strip()
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

