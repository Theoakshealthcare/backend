"""The Gemini implementation of ``TranscriptionProvider``. The only module that imports the SDK.

It has never been exercised against the real service in this repository (no key, no network in tests). Everything about what Gemini
accepts (``audio/mp4`` in particular, plan risk R3), what it returns and how it fails is written from the SDK's documented shapes
and is unverified until a live smoke run.

Nothing here logs a prompt, an answer, an audio byte or an exception message: only the failure class name.
"""

import asyncio
import logging
from typing import Any, Optional

from app.core.config import settings
from app.services.voice_notes import prompts
from app.services.voice_notes.provider import (
    NoSpeech, ProviderBlocked, ProviderRefused, ProviderTimeout, ProviderTransient, Summary, Transcription,
)

log = logging.getLogger(__name__)

# The SDK and its HTTP stack can log request details at INFO/DEBUG; keep them quiet (plan section 6.5).
for _noisy in ("google_genai", "google.genai", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_REFUSED_CODES = {400, 413, 415, 422}


class GeminiProvider:
    name = "gemini"

    def __init__(self, client: Optional[Any] = None) -> None:
        self._client = client

    @property
    def model(self) -> str:
        return settings.VOICE_NOTE_MODEL

    def _get_client(self):
        if self._client is None:
            if not settings.GEMINI_API_KEY:
                raise ProviderTransient()  # not configured: the note fails as "busy", the operator sees the class name in the log
            from google import genai

            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    async def _generate(self, contents: list, system: str) -> Any:
        from google.genai import errors, types

        client = self._get_client()
        config = types.GenerateContentConfig(system_instruction=system, temperature=0)
        try:
            return await asyncio.wait_for(
                client.aio.models.generate_content(model=self.model, contents=contents, config=config),
                timeout=settings.VOICE_NOTE_PROVIDER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise ProviderTimeout() from None
        except errors.APIError as exc:
            code = getattr(exc, "code", None)
            log.warning("voice note provider error: %s code=%s", type(exc).__name__, code)
            if code in _REFUSED_CODES:
                raise ProviderRefused() from None
            raise ProviderTransient() from None  # 429, 5xx, a bad key (an operator problem): all "try again later"
        except ProviderTransient:
            raise
        except Exception as exc:  # network and anything unexpected; never log the message, it may echo content
            log.warning("voice note provider failure: %s", type(exc).__name__)
            if "Timeout" in type(exc).__name__:
                raise ProviderTimeout() from None
            raise ProviderTransient() from None

    @staticmethod
    def _text_of(response: Any) -> str:
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise ProviderBlocked()
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            reason = str(getattr(candidates[0], "finish_reason", "") or "")
            if any(word in reason.upper() for word in ("SAFETY", "PROHIBITED", "BLOCKLIST", "RECITATION")):
                raise ProviderBlocked()
        return (getattr(response, "text", None) or "").strip()

    @staticmethod
    def _tokens(response: Any):
        usage = getattr(response, "usage_metadata", None)
        return getattr(usage, "prompt_token_count", None), getattr(usage, "candidates_token_count", None)

    async def transcribe(self, audio: bytes, mime: str, language: str) -> Transcription:
        from google.genai import types

        contents = [types.Part.from_bytes(data=audio, mime_type=mime), prompts.LANGUAGE_HINTS.get(language, prompts.LANGUAGE_HINTS["en"])]
        response = await self._generate(contents, prompts.TRANSCRIBE_SYSTEM)
        text = self._text_of(response)
        if not text or text == prompts.NO_SPEECH_MARKER:
            raise NoSpeech()
        tokens_in, tokens_out = self._tokens(response)
        return Transcription(text=text, tokens_in=tokens_in, tokens_out=tokens_out)

    async def summarize(self, transcript: str, language: str) -> Summary:
        response = await self._generate([f"<transcript>\n{transcript}\n</transcript>"], prompts.SUMMARY_SYSTEM)
        text = self._text_of(response)
        if not text:
            raise ProviderTransient()
        tokens_in, tokens_out = self._tokens(response)
        return Summary(text=text[:600], tokens_in=tokens_in, tokens_out=tokens_out)
