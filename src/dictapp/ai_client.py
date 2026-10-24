import json
import logging

import httpx
from fastapi import HTTPException

from dictapp.settings import settings


logger = logging.getLogger("uvicorn.error")


async def request_ai_json(prompt: str) -> dict:
    api_key = settings.ai_api_key.get_secret_value().strip()

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="API-ключ ИИ не настроен.",
        )

    payload = {
        "model": settings.ai_model,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": settings.ai_reasoning_effort,
        "max_tokens": settings.ai_max_tokens,
    }

    timeout = httpx.Timeout(
        settings.ai_timeout_seconds,
        connect=10.0,
    )

    # Один запрос, без автоматических повторов.
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"{settings.ai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            response.raise_for_status()

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Истекло время ожидания ответа ИИ.",
        ) from None

    except httpx.HTTPStatusError as error:
        code = error.response.status_code

        if code in (401, 403):
            detail = "Selectel отклонил доступ. Проверь API-ключ и его права."
        elif code == 429:
            detail = "Selectel сообщил о превышении лимита запросов."
        else:
            detail = f"Selectel вернул ошибку HTTP {code}."

        raise HTTPException(
            status_code=429 if code == 429 else 502,
            detail=detail,
        ) from None

    except httpx.RequestError:
        raise HTTPException(
            status_code=502,
            detail="Не удалось соединиться с Selectel.",
        ) from None

    try:
        data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Selectel вернул некорректный ответ API.",
        ) from None

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail="Неожиданный формат ответа API.",
        )

    usage = data.get("usage")
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
        reasoning = (
            details.get("reasoning_tokens")
            if isinstance(details, dict)
            else None
        )

        logger.info(
            "AI tokens: input=%s, output=%s, reasoning=%s",
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            reasoning,
        )

    choices = data.get("choices")
    if (
        not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
    ):
        raise HTTPException(
            status_code=502,
            detail="В ответе API отсутствует результат модели.",
        )

    choice = choices[0]

    if choice.get("finish_reason") == "length":
        raise HTTPException(
            status_code=502,
            detail="Ответ ИИ обрезан по лимиту токенов.",
        )

    if choice.get("finish_reason") != "stop":
        raise HTTPException(
            status_code=502,
            detail="ИИ не завершил ответ обычным способом.",
        )

    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None

    if not isinstance(content, str) or not content.strip():
        raise HTTPException(
            status_code=502,
            detail="ИИ вернул пустой ответ.",
        )

    try:
        result = json.loads(content)
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="ИИ вернул ответ не в формате JSON.",
        ) from None

    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="ИИ вернул JSON неподходящей структуры.",
        )

    return result