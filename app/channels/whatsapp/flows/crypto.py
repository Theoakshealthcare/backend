"""Encryption for the WhatsApp Flows data endpoint.

Meta encrypts every request to the endpoint and expects every response encrypted the same way:
  https://developers.facebook.com/docs/whatsapp/flows/guides/implementingyourflowendpoint#data_api_request_encryption
  https://developers.facebook.com/docs/whatsapp/flows/guides/implementingyourflowendpoint#data_api_response_encryption

Request: JSON ``{"encrypted_flow_data", "encrypted_aes_key", "initial_vector"}``, all base64.
- ``encrypted_aes_key`` is a fresh 128-bit AES key wrapped with our RSA public key (RSA-OAEP, SHA-256 for both
  the hash and MGF1, no label).
- ``encrypted_flow_data`` is the JSON payload under AES-128-GCM with that key and ``initial_vector``; the last 16
  bytes are the GCM tag.
Response: the response JSON under the same key, encrypted with the request IV **bitwise flipped** (every byte
XOR 0xFF), ciphertext followed by the tag, base64 encoded, sent as the plain-text body.

GCM's tag makes a tampered ciphertext, tag or IV fail to decrypt; none of those become a partial payload.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TAG_BYTES = 16
AES_KEY_BYTES = 16  # AES-128
MIN_IV_BYTES = 12


class FlowCryptoError(Exception):
    """The request could not be decrypted. The endpoint answers HTTP 421 so Meta re-fetches our public key."""


@dataclass(frozen=True)
class DecryptedRequest:
    payload: Dict[str, Any]
    aes_key: bytes
    iv: bytes


def load_private_key(pem: str, passphrase: Optional[str] = None) -> RSAPrivateKey:
    if not pem or not pem.strip():
        raise FlowCryptoError("no Flow private key is configured")
    data = pem.replace("\\n", "\n").strip().encode()  # an env file keeps the key on one line with literal \n
    try:
        key = serialization.load_pem_private_key(data, password=passphrase.encode() if passphrase else None)
    except (ValueError, TypeError) as exc:
        raise FlowCryptoError("the Flow private key could not be loaded") from exc
    if not isinstance(key, RSAPrivateKey):
        raise FlowCryptoError("the Flow private key is not an RSA key")
    return key


def _b64(body: Dict[str, Any], field: str) -> bytes:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise FlowCryptoError(f"{field} is missing")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FlowCryptoError(f"{field} is not base64") from exc


def decrypt_request(body: Any, private_key: RSAPrivateKey) -> DecryptedRequest:
    if not isinstance(body, dict):
        raise FlowCryptoError("the request is not a JSON object")
    wrapped = _b64(body, "encrypted_aes_key")
    iv = _b64(body, "initial_vector")
    data = _b64(body, "encrypted_flow_data")
    if len(iv) < MIN_IV_BYTES or len(data) <= TAG_BYTES:
        raise FlowCryptoError("the request is too short")
    try:
        aes_key = private_key.decrypt(
            wrapped,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
    except ValueError as exc:
        raise FlowCryptoError("the AES key could not be unwrapped") from exc
    if len(aes_key) != AES_KEY_BYTES:
        raise FlowCryptoError("the AES key is not 128 bits")
    try:
        plaintext = AESGCM(aes_key).decrypt(iv, data, None)  # AESGCM expects ciphertext || tag, as Meta sends it
    except InvalidTag as exc:
        raise FlowCryptoError("authentication failed") from exc
    try:
        payload = json.loads(plaintext)
    except ValueError as exc:
        raise FlowCryptoError("the payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise FlowCryptoError("the payload is not a JSON object")
    return DecryptedRequest(payload=payload, aes_key=aes_key, iv=iv)


def flip_iv(iv: bytes) -> bytes:
    return bytes(b ^ 0xFF for b in iv)


def encrypt_response(response: Dict[str, Any], request: DecryptedRequest) -> str:
    sealed = AESGCM(request.aes_key).encrypt(flip_iv(request.iv), json.dumps(response).encode(), None)
    return base64.b64encode(sealed).decode()
