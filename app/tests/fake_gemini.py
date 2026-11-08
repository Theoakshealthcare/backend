"""A fake voice note transcription provider (docs/plans/2026-11-07-ai-note-transcription.md section 6.7), in the style of fake_dojah.

No network, scripted and deterministic. It stands in for ``GeminiProvider`` through ``set_provider_for_tests``. Every call is recorded
as ``(kind, mime, length, sha256, language)`` so a test can assert "sent once, with these bytes" without keeping the audio. The defaults
carry sentinel strings (``SENTINEL_*``) so the privacy tests can grep logs, tables and audit rows for them.

The REAL Gemini call is not exercised anywhere in this repository (no key, no network). Whatever the real service accepts or answers is
unverified until a live smoke run (plan risk R3).
"""
import asyncio
import hashlib
from typing import Dict, List, Optional

from app.services.voice_notes.provider import (
    NoSpeech, ProviderBlocked, ProviderRefused, ProviderTimeout, ProviderTransient, Summary, Transcription,
)

SENTINEL_AUDIO = b"SENTINEL-AUDIO-do-not-store"
SENTINEL_TRANSCRIPT = "SENTINEL-TRANSCRIPT-do-not-log: Mrs A had her tea at nine. Blood pressure one twenty over eighty."
SENTINEL_SUMMARY = "SENTINEL-SUMMARY-do-not-log: tea at nine; blood pressure 120/80."
SENTINEL_PROVIDER_MESSAGE = "SENTINEL-PROVIDER-MESSAGE-do-not-log"
SENTINELS = (SENTINEL_AUDIO.decode(), "SENTINEL-TRANSCRIPT-do-not-log", "SENTINEL-SUMMARY-do-not-log", SENTINEL_PROVIDER_MESSAGE)

# A tiny but plausible m4a header (``ftyp`` at byte 4) carrying the sentinel, so the container check passes.
FAKE_AUDIO = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + SENTINEL_AUDIO + bytes(64)


class FakeGemini:
    name = "fake-gemini"
    model = "fake-gemini-model"

    def __init__(self, transcripts: Optional[List[str]] = None, summaries: Optional[List[str]] = None, delay_seconds: float = 0.0) -> None:
        self.transcripts = list(transcripts or [])
        self.summaries = list(summaries or [])
        self.delay_seconds = delay_seconds
        self.calls: List[Dict[str, object]] = []
        self._failures: List[tuple] = []  # (target, kind)

    def fail_next(self, kind: str, times: int = 1, target: str = "transcribe") -> None:
        """``kind``: transient, timeout, refused, blocked, no_speech, garbage, empty, crash. ``target``: transcribe or summarize."""
        self._failures.extend([(target, kind)] * times)

    def _next_failure(self, target: str) -> Optional[str]:
        for i, (t, kind) in enumerate(self._failures):
            if t == target:
                del self._failures[i]
                return kind
        return None

    def calls_of(self, kind: str) -> List[Dict[str, object]]:
        return [c for c in self.calls if c["kind"] == kind]

    async def transcribe(self, audio: bytes, mime: str, language: str) -> Transcription:
        self.calls.append({"kind": "transcribe", "mime": mime, "length": len(audio), "sha256": hashlib.sha256(audio).hexdigest(), "language": language})
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        kind = self._next_failure("transcribe")
        if kind == "transient":
            raise ProviderTransient()
        if kind == "timeout":
            raise ProviderTimeout()
        if kind == "refused":
            raise ProviderRefused()
        if kind == "blocked":
            raise ProviderBlocked()
        if kind == "no_speech":
            raise NoSpeech()
        if kind == "crash":
            raise RuntimeError(SENTINEL_PROVIDER_MESSAGE)
        if kind == "garbage":
            return Transcription(text="x" * 50_000)
        if kind == "empty":
            return Transcription(text="   ")
        text = self.transcripts.pop(0) if self.transcripts else SENTINEL_TRANSCRIPT
        return Transcription(text=text, tokens_in=1000, tokens_out=40)

    async def summarize(self, transcript: str, language: str) -> Summary:
        self.calls.append({"kind": "summarize", "mime": None, "length": len(transcript), "sha256": None, "language": language})
        kind = self._next_failure("summarize")
        if kind == "transient":
            raise ProviderTransient()
        if kind == "timeout":
            raise ProviderTimeout()
        if kind == "crash":
            raise RuntimeError(SENTINEL_PROVIDER_MESSAGE)
        text = self.summaries.pop(0) if self.summaries else SENTINEL_SUMMARY
        return Summary(text=text, tokens_in=60, tokens_out=20)
