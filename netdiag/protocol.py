"""Authenticated fixed-size synthetic probes; never carries application traffic."""

import hashlib
import hmac
import os
import secrets
import struct
import time

BODY = struct.Struct("!4scQ16sI32s")
FRAME_SIZE = BODY.size + 32
MAX_AGE = 30


def load_key():
    key = os.environ.get("NETDIAG_KEY", "").encode()
    if len(key) < 32:
        raise ValueError("NETDIAG_KEY must contain at least 32 bytes of random secret material")
    return key


def make_request(key, sequence=0):
    body = BODY.pack(b"NDG1", b"Q", int(time.time() * 1000),
                     secrets.token_bytes(16), sequence, secrets.token_bytes(32))
    return body + hmac.digest(key, body, hashlib.sha256)


def parse_frame(frame, key, kind, max_age=MAX_AGE):
    if len(frame) != FRAME_SIZE:
        raise ValueError("invalid frame size")
    body, signature = frame[:-32], frame[-32:]
    if not hmac.compare_digest(signature, hmac.digest(key, body, hashlib.sha256)):
        raise ValueError("invalid authentication")
    magic, actual_kind, timestamp, nonce, sequence, payload = BODY.unpack(body)
    if magic != b"NDG1" or actual_kind != kind:
        raise ValueError("invalid frame type")
    if abs(time.time() - timestamp / 1000) > max_age:
        raise ValueError("expired frame or unsynchronized clocks")
    return timestamp, nonce, sequence, payload


def make_response(request, key):
    fields = parse_frame(request, key, b"Q")
    body = BODY.pack(b"NDG1", b"R", *fields)
    return body + hmac.digest(key, body, hashlib.sha256)


def verify_response(response, request, key):
    if parse_frame(response, key, b"R") != parse_frame(request, key, b"Q"):
        raise ValueError("response does not match request")
