"""The transcription provider interface (plan section 6.1). Gemini is the one real implementation (``gemini_provider``); tests use
``app/tests/fake_gemini.py``. Nothing outside the provider modules imports an AI SDK."""

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class Transcription:
    text: str  # the transcript; an empty answer is not allowed, raise NoSpeech instead
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


@dataclass(frozen=True)
class Summary:
    text: str
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


class ProviderTransient(Exception):
    """429, 5xx, network: worth trying again, then ``provider_unavailable``."""


class ProviderTimeout(Exception):
    """The call took too long: ``transcription_timeout``."""


class ProviderRefused(Exception):
    """The provider would not accept the file: ``audio_unsupported``."""


class ProviderBlocked(Exception):
    """The provider's safety filter refused it: ``provider_blocked``."""


class NoSpeech(Exception):
    """Nothing intelligible was said: ``no_speech_detected``."""


class TranscriptionProvider(Protocol):
    name: str
    model: str

    async def transcribe(self, audio: bytes, mime: str, language: str) -> Transcription: ...

    async def summarize(self, transcript: str, language: str) -> Summary: ...


_provider: Optional[TranscriptionProvider] = None
_override: Optional[TranscriptionProvider] = None


def get_transcription_provider() -> TranscriptionProvider:
    """The provider the worker uses: Gemini, built on first use (the client refuses to construct without a key)."""
    if _override is not None:
        return _override
    global _provider
    if _provider is None:
        from app.services.voice_notes.gemini_provider import GeminiProvider

        _provider = GeminiProvider()
    return _provider


def set_provider_for_tests(provider: Optional[TranscriptionProvider]) -> None:
    global _override
    _override = provider
