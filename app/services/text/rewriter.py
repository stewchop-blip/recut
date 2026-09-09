"""Text rewriting service using OpenRouter LLM."""

import httpx
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger


logger = get_logger(__name__)

# System prompt for text preparation
REWRITE_SYSTEM_PROMPT = (
    "Ты — редактор текстов для озвучки. Твоя задача: сделать текст звучащим естественно "
    "при чтении вслух, сохраняя смысл и факты.\n\n"
    "ПРАВИЛА:\n"
    "1. Сохраняй факты, цифры, имена, даты, URL, названия.\n"
    "2. Убирай канцелярит, лишние вводные слова, пассивные конструкции.\n"
    "3. Укорочи длинные предложения. Разбивай их на более короткие.\n"
    "4. Используй живой, разговорный русский язык.\n"
    "5. Улучшай ритм — текст должен хорошо звучать при озвучке.\n"
    "6. Не добавляй новых фактов, не выдумывай детали.\n"
    "7. Не меняй смысл. Не добавляй ложные утверждения.\n"
    "8. Избегай излишних заголовков, списков и структуры — это речь, а не статья.\n"
    "9. Не оптимизируй специально под AI-детекторы.\n\n"
    "Верни ТОЛЬКО переписанный текст, без пояснений и форматирования."
)


class TextRewriter:
    """Rewrites text for natural voiceover using OpenRouter LLM."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self._api_key = api_key or get_settings().openrouter_api_key
        self._model = model or get_settings().openrouter_rewrite_model
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://openrouter.ai/api/v1",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://recut.bot",
                    "X-Title": "Recut Bot",
                },
                timeout=httpx.Timeout(self._timeout, connect=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def rewrite(self, text: str) -> str:
        """
        Rewrite text for natural voiceover.

        Args:
            text: Original text

        Returns:
            Rewritten text

        Raises:
            RewriteError: On failure
        """
        if not text.strip():
            raise RewriteError("Text cannot be empty")

        if len(text) > get_settings().max_text_length:
            raise RewriteError(f"Text too long: {len(text)} > {get_settings().max_text_length}")

        client = await self._get_client()

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
            "max_tokens": min(len(text) * 2, 4000),  # Generous but bounded
        }

        try:
            logger.debug("rewrite_request", model=self._model, text_length=len(text))

            response = await client.post("/chat/completions", json=payload)

            if response.status_code != 200:
                error_data = {}
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {"raw": response.text[:500]}

                error_msg = error_data.get("error", {}).get("message", f"HTTP {response.status_code}")
                logger.error("rewrite_failed", status=response.status_code, error=error_msg)

                retryable = response.status_code in (429, 500, 502, 503, 504)
                raise RewriteError(
                    f"Rewrite failed: {error_msg}",
                    code=f"HTTP_{response.status_code}",
                    retryable=retryable,
                )

            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                raise RewriteError("Empty response from LLM", code="EMPTY_RESPONSE")

            rewritten = choices[0].get("message", {}).get("content", "").strip()
            if not rewritten:
                raise RewriteError("Empty rewritten text", code="EMPTY_CONTENT")

            logger.info("rewrite_success", model=self._model, original_len=len(text), rewritten_len=len(rewritten))
            return rewritten

        except httpx.TimeoutException as e:
            logger.error("rewrite_timeout", error=str(e))
            raise RewriteError("Rewrite request timed out", code="TIMEOUT", retryable=True) from e

        except httpx.RequestError as e:
            logger.error("rewrite_request_error", error=str(e))
            raise RewriteError(f"Rewrite request failed: {e}", code="REQUEST_ERROR", retryable=True) from e


class RewriteError(Exception):
    """Text rewriting error."""

    def __init__(self, message: str, code: str = "REWRITE_ERROR", retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(message)


# Global instance
text_rewriter = TextRewriter()


def get_text_rewriter() -> TextRewriter:
    return text_rewriter