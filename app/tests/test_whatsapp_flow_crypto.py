"""W2: the Flow data endpoint's encryption (RSA-OAEP key wrap, AES-128-GCM payload, flipped-IV response)."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.channels.whatsapp.flows.crypto import FlowCryptoError, decrypt_request, encrypt_response, load_private_key
from app.tests.fake_meta import FlowClient, make_flow_keypair


@pytest.fixture(scope="module")
def keys():
    return make_flow_keypair()


def test_round_trip_request_and_response(keys):
    private_pem, public_pem = keys
    client = FlowClient(public_pem)
    body = client.encrypt({"version": "3.0", "action": "ping"})
    decrypted = decrypt_request(body, load_private_key(private_pem))
    assert decrypted.payload == {"version": "3.0", "action": "ping"}
    response = encrypt_response({"version": "3.0", "data": {"status": "active"}}, decrypted)
    assert client.decrypt_response(response) == {"version": "3.0", "data": {"status": "active"}}


def test_response_uses_the_flipped_iv_not_the_request_iv(keys):
    private_pem, public_pem = keys
    client = FlowClient(public_pem)
    decrypted = decrypt_request(client.encrypt({"action": "ping"}), load_private_key(private_pem))
    response = encrypt_response({"ok": True}, decrypted)
    # Decrypting with the request IV (the classic mistake) must fail: that is what makes the flip load-bearing.
    with pytest.raises(Exception):
        client.decrypt_response(response, iv=decrypted.iv)


def _flip_a_bit(body, field, index):
    raw = bytearray(base64.b64decode(body[field]))
    raw[index] ^= 0x01
    body[field] = base64.b64encode(bytes(raw)).decode()


@pytest.mark.parametrize("field,index", [("encrypted_flow_data", 0), ("encrypted_flow_data", -1),
                                         ("initial_vector", 0), ("encrypted_aes_key", 5)])
def test_any_tampered_part_is_rejected(keys, field, index):
    private_pem, public_pem = keys
    body = FlowClient(public_pem).encrypt({"action": "ping"})
    _flip_a_bit(body, field, index)
    with pytest.raises(FlowCryptoError):
        decrypt_request(body, load_private_key(private_pem))


def test_a_key_wrapped_for_another_public_key_is_rejected(keys):
    private_pem, _ = keys
    _, other_public = make_flow_keypair()
    body = FlowClient(other_public).encrypt({"action": "ping"})
    with pytest.raises(FlowCryptoError):
        decrypt_request(body, load_private_key(private_pem))


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("encrypted_aes_key"),
    lambda b: b.pop("initial_vector"),
    lambda b: b.pop("encrypted_flow_data"),
    lambda b: b.update(encrypted_flow_data="not base64 !!"),
    lambda b: b.update(encrypted_flow_data=base64.b64encode(b"short").decode()),
    lambda b: b.update(encrypted_aes_key=123),
    lambda b: b.update(initial_vector=""),
])
def test_malformed_envelopes_are_rejected_not_crashed(keys, mutate):
    private_pem, public_pem = keys
    body = FlowClient(public_pem).encrypt({"action": "ping"})
    mutate(body)
    with pytest.raises(FlowCryptoError):
        decrypt_request(body, load_private_key(private_pem))


def test_a_payload_that_is_not_a_json_object_is_rejected(keys):
    private_pem, public_pem = keys
    body = FlowClient(public_pem).encrypt_raw(b"[1, 2, 3]")
    with pytest.raises(FlowCryptoError):
        decrypt_request(body, load_private_key(private_pem))


def test_an_aes_key_of_the_wrong_length_is_rejected(keys):
    private_pem, public_pem = keys
    body = FlowClient(public_pem).encrypt({"action": "ping"}, aes_key=b"k" * 24)
    with pytest.raises(FlowCryptoError):
        decrypt_request(body, load_private_key(private_pem))


def test_passphrase_protected_keys_load():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(b"s3cret-placeholder")).decode()
    assert load_private_key(pem, "s3cret-placeholder").key_size == 2048
    with pytest.raises(FlowCryptoError):
        load_private_key(pem, "wrong")
    with pytest.raises(FlowCryptoError):
        load_private_key(pem, "")


def test_env_style_escaped_newlines_are_accepted():
    private_pem, _ = make_flow_keypair()
    one_line = private_pem.strip().replace("\n", "\\n")
    assert load_private_key(one_line).key_size == 2048


def test_an_empty_or_garbage_key_is_an_error():
    with pytest.raises(FlowCryptoError):
        load_private_key("")
    with pytest.raises(FlowCryptoError):
        load_private_key("not a pem")


def test_decrypted_payload_is_plain_json(keys):
    private_pem, public_pem = keys
    payload = {"action": "data_exchange", "screen": "WHO", "data": {"who": "self"}, "flow_token": "t"}
    decrypted = decrypt_request(FlowClient(public_pem).encrypt(payload), load_private_key(private_pem))
    assert json.loads(json.dumps(decrypted.payload)) == payload
