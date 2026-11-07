"""Where the audio waits between the upload and the transcript (plan section 5.2).

Redis, under ``vn:audio:<note id>``, with a TTL. Redis runs with an append-only file in production, so the bytes are **encrypted before
they are stored** (``park_audio``) and a stored value is never plaintext audio. The key is deleted the moment the text exists; the TTL
is the backstop. The server writes no audio file, no temp file and no object-storage upload at any point.

``SlotLimiter`` rides on the same Redis: at most ``VOICE_NOTE_MAX_CONCURRENT`` transcriptions at once, so the shared worker keeps slots
for payments and dispatch (plan risk R9).
"""

import time
import uuid
from typing import Callable, Dict, List, Optional, Protocol, Tuple

from app.core.config import settings
from app.core.security import decrypt_bytes, encrypt_bytes

KEY_PREFIX = "vn:audio:"
SLOTS_KEY = "vn:slots"
SLOT_TTL_SECONDS = 300  # a worker that died holding a slot frees it by the key expiring


class AudioStore(Protocol):
    def put(self, note_id: str, ciphertext: bytes, ttl_seconds: int) -> None: ...
    def get(self, note_id: str) -> Optional[bytes]: ...
    def delete(self, note_id: str) -> None: ...
    def exists(self, note_id: str) -> bool: ...
    def touch(self, note_id: str, ttl_seconds: int) -> None: ...
    def list_ids(self) -> List[str]: ...
    def acquire_slot(self, limit: int) -> Optional[str]: ...
    def release_slot(self, token: str) -> None: ...


class RedisAudioStore:
    def __init__(self, url: Optional[str] = None) -> None:
        self._url = url
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis  # a dependency of arq; imported late so importing this module never needs a server

            self._client = redis.Redis.from_url(self._url or settings.REDIS_URL, socket_connect_timeout=3, socket_timeout=10)
        return self._client

    def put(self, note_id: str, ciphertext: bytes, ttl_seconds: int) -> None:
        self._redis().set(KEY_PREFIX + note_id, ciphertext, ex=ttl_seconds)

    def get(self, note_id: str) -> Optional[bytes]:
        return self._redis().get(KEY_PREFIX + note_id)

    def delete(self, note_id: str) -> None:
        self._redis().delete(KEY_PREFIX + note_id)

    def exists(self, note_id: str) -> bool:
        return bool(self._redis().exists(KEY_PREFIX + note_id))

    def touch(self, note_id: str, ttl_seconds: int) -> None:
        self._redis().expire(KEY_PREFIX + note_id, ttl_seconds)

    def list_ids(self) -> List[str]:
        return [key.decode()[len(KEY_PREFIX):] for key in self._redis().scan_iter(match=KEY_PREFIX + "*", count=200)]

    def acquire_slot(self, limit: int) -> Optional[str]:
        client = self._redis()
        if client.incr(SLOTS_KEY) > limit:
            client.decr(SLOTS_KEY)
            return None
        client.expire(SLOTS_KEY, SLOT_TTL_SECONDS)
        return uuid.uuid4().hex

    def release_slot(self, token: str) -> None:
        client = self._redis()
        if client.decr(SLOTS_KEY) < 0:
            client.set(SLOTS_KEY, 0)


class MemoryAudioStore:
    """The same behaviour in a dict, with an injectable clock. For tests and the capture run."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._items: Dict[str, Tuple[bytes, float]] = {}
        self._slots = 0

    def _alive(self, note_id: str) -> Optional[bytes]:
        item = self._items.get(note_id)
        if item is None:
            return None
        if item[1] <= self._clock():
            del self._items[note_id]
            return None
        return item[0]

    def put(self, note_id: str, ciphertext: bytes, ttl_seconds: int) -> None:
        self._items[note_id] = (ciphertext, self._clock() + ttl_seconds)

    def get(self, note_id: str) -> Optional[bytes]:
        return self._alive(note_id)

    def delete(self, note_id: str) -> None:
        self._items.pop(note_id, None)

    def exists(self, note_id: str) -> bool:
        return self._alive(note_id) is not None

    def touch(self, note_id: str, ttl_seconds: int) -> None:
        data = self._alive(note_id)
        if data is not None:
            self._items[note_id] = (data, self._clock() + ttl_seconds)

    def list_ids(self) -> List[str]:
        return [note_id for note_id in list(self._items) if self._alive(note_id) is not None]

    def acquire_slot(self, limit: int) -> Optional[str]:
        if self._slots >= limit:
            return None
        self._slots += 1
        return uuid.uuid4().hex

    def release_slot(self, token: str) -> None:
        self._slots = max(0, self._slots - 1)

    def raw_values(self) -> List[bytes]:
        """For tests: every stored value, as stored."""
        return [value for value, _ in self._items.values()]


_store: Optional[AudioStore] = None
_override: Optional[AudioStore] = None


def get_audio_store() -> AudioStore:
    if _override is not None:
        return _override
    global _store
    if _store is None:
        _store = RedisAudioStore()
    return _store


def set_audio_store_for_tests(store: Optional[AudioStore]) -> None:
    global _override
    _override = store


def park_audio(store: AudioStore, note_id: str, audio: bytes) -> None:
    """Encrypt and store. A failure (Redis down) raises; nothing plaintext is left behind."""
    store.put(note_id, encrypt_bytes(audio), settings.VOICE_NOTE_AUDIO_TTL_SECONDS)


def read_audio(store: AudioStore, note_id: str) -> Optional[bytes]:
    stored = store.get(note_id)
    return None if stored is None else decrypt_bytes(stored)
