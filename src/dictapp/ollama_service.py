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

Отвечай ТОЛЬКО на русском языке.
Не пиши длинные словарные статьи.
Главная задача: перевести предложение ТОЧНО, а не вольно.

Сначала дай БУКВАЛЬНЫЙ перевод, максимально близкий к китайскому тексту.
Потом дай ЕСТЕСТВЕННЫЙ перевод на хорошем русском.
Не заменяй буквальный перевод пересказом.
Если в предложении есть разговорность, укажи это отдельно.

Если в предложении используется мат, грубая лексика или оскорбления,
переводи их честно и прямо.

Не смягчай ругательства и не заменяй их нейтральными словами.
Не цензурируй перевод.

Если в оригинале используется грубая или вульгарная речь,
перевод на русский должен передавать ту же степень грубости.

Верни ответ СТРОГО в таком формате:

Буквальный перевод:
...

Естественный перевод:
...

Пиньинь:
...

Ключевые слова:
- ...
- ...
- ...

Пояснение:
...

Предложение:
{text}

Словарные данные:
{dictionary_context}
""".strip()

async def analyze_with_ollama(text: str, dictionary_entries: list[Entry]) -> str:
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

    return (data.get("response") or "").strip()



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