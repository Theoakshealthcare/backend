"""Statuses, stable error codes and their sentences (plan section 3.8). The codes are the contract: the app branches on them."""

from typing import Dict, Tuple

STATUS_AWAITING = "awaiting_audio"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_SAVED = "saved"

CONSENT_VERSIONS = frozenset({"2026-11-v1"})  # the copy the app showed (plan section 11); a new wording is a new entry
LANGUAGES = ("en", "pcm")

# Allowed audio types and what the first bytes of each must look like (plan section 5.1).
AUDIO_TYPES = ("audio/mp4", "audio/x-m4a", "audio/aac", "audio/mpeg", "audio/wav")

TRANSCRIPT_MAX_CHARS = 20_000
SUMMARY_MAX_CHARS = 2_000
PROVIDER_TEXT_MAX_CHARS = 40_000  # an answer longer than this is not a transcript

# HTTP errors: ``{"detail": {"code", "message"}}``.
REQUEST_ERRORS: Dict[str, str] = {
    "consent_required": "Confirm the client agreed before recording.",
    "consent_version_unknown": "Update the Oakly app to record notes.",
    "audio_empty": "The recording is empty. Please record again.",
    "not_found": "This note could not be found.",
    "visit_not_active": "Voice notes can be added during a visit.",
    "consent_missing": "Confirm the client agreed before recording.",
    "already_processing": "This recording is already being processed.",
    "invalid_state": "This note changed. Please refresh.",
    "not_ready": "The note isn't ready to save yet.",
    "already_saved": "This note is already saved.",
    "audio_expired": "The recording is no longer stored. Please send it again.",
    "audio_too_large": "That recording is too long. Keep notes under 10 minutes.",
    "unsupported_audio_type": "This recording format isn't supported.",
    "note_limit_reached": "You have reached the limit of voice notes for this visit.",
    "daily_limit_reached": "You have reached today's limit of voice notes. Please type your note.",
    "transcription_unavailable": "Voice notes are unavailable right now. Please type your note.",
}

# Failure codes carried in ``error.code`` of a failed note: (message, retryable).
FAILURES: Dict[str, Tuple[str, bool]] = {
    "transcription_failed": ("We couldn't turn the recording into text. Try again.", True),
    "transcription_timeout": ("Transcription took too long. Try again.", True),
    "provider_unavailable": ("The voice service is busy. Try again in a moment.", True),
    "no_speech_detected": ("We couldn't hear any speech. Record again, or type your note.", False),
    "audio_unsupported": ("We couldn't read this recording. Record again, or type your note.", False),
    "provider_blocked": ("We couldn't process this recording. Please type your note.", False),
}
