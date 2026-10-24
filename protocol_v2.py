# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast
# Implementation and development by Srishti Saluja
"""Versioned binary wire protocol (v2) for terminal canvas messages.

v1 (protocol.py) stays unchanged and remains the default; v2 is selected
with ``python app.py --protocol 2``. The two versions do not interoperate:
a v2 peer drops v1 frames (and counts them), and a v1 peer drops every v2
frame because it is not 13 bytes long.

Every v2 datagram is a 20-byte header followed by a type-specific payload.
All integers are big-endian; header struct format ``!HBBBBIIIH``::

    offset  size  field     struct  meaning
    0       2     magic     H       0xCA55, as in v1
    2       1     version   B       2
    3       1     type      B       MessageType code
    4       1     flags     B       bit 0 AUTH (HMAC tag follows the payload),
                                    bit 1 RETRANSMIT; other bits must be 0
    5       1     reserved  B       must be 0
    6       4     peer      I       random per-session sender ID, never 0
    10      4     sequence  I       per-sender counter (see below)
    14      4     lamport   I       sender's Lamport timestamp
    18      2     length    H       payload bytes (excluding any HMAC tag)

With authentication (auth.py) the AUTH flag is set and a 16-byte HMAC tag
over header + payload follows the payload; the length field still counts
the payload only.

Only DRAW and CLEAR consume a sequence number. Every other type carries the
sender's *next* sequence number without consuming it, so a receiver can
spot events it has not seen.

Messages are plain dicts, like v1 events, with "peer", "seq", "lamport" and
"retransmit" added:

    {"type": "DRAW", "peer": 0x1A2B3C4D, "seq": 7, "lamport": 0,
     "retransmit": False, "x": 20, "y": 10, "char": "#", "color": 2}

Late-join payloads (see sync.py):

    STATE_REQ   nonce I                                         4 bytes
    STATE_RESP  nonce I, index H, count H, crc32 I, width H,
                height H, clear lamport I, clear peer I         24 bytes
                then 0-80 cell records of x h, y h, char c,
                colour B, lamport I, peer I                     14 bytes each

Loss-recovery payloads (see recovery.py):

    HEARTBEAT      none (the header's sequence is the sender's next number)
    NACK           target peer I, first seq I, count H         10 bytes
    NOT_AVAILABLE  first seq I, count H, oldest available I     10 bytes

    {"type": "NACK", ..., "target": 0x0BADF00D, "first": 12, "count": 3}
    {"type": "NOT_AVAILABLE", ..., "first": 2, "count": 1, "oldest": 40}

    {"type": "STATE_REQ", ..., "nonce": 99}
    {"type": "STATE_RESP", ..., "nonce": 99, "index": 0, "count": 2,
     "crc": 0x1234ABCD, "width": 60, "height": 18, "clear_stamp": (4, 7),
     "cells": [(x, y, char, color, lamport, peer), ...]}
"""

from __future__ import annotations

import enum
import secrets
import struct
from typing import Optional

import auth
import protocol
from protocol import ProtocolError

MAGIC = protocol.MAGIC
VERSION = 2

HEADER_FORMAT = "!HBBBBIIIH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 20

FLAG_AUTH = 0x01
FLAG_RETRANSMIT = 0x02
KNOWN_FLAGS = FLAG_AUTH | FLAG_RETRANSMIT

MAX_U32 = 2**32 - 1
MAX_U16 = 2**16 - 1
MAX_COORDINATE = protocol.MAX_COORDINATE
MAX_COLOR = protocol.MAX_COLOR

# Keeps every datagram well below a 1,500-byte Ethernet MTU once the UDP (8),
# IPv4 (20) headers and an optional 16-byte HMAC tag are added.
MAX_PAYLOAD = 1200

DRAW = protocol.DRAW
CLEAR = protocol.CLEAR


class LegacyFrameError(ProtocolError):
    """Raised for a v1 frame (13 bytes, v1 magic) received in v2 mode."""


class AuthenticationError(ProtocolError):
    """Raised for a missing, malformed or wrong HMAC tag, or a tagged frame
    received without a key."""


class MessageType(enum.IntEnum):
    DRAW = 1
    CLEAR = 2
    STATE_REQ = 3
    STATE_RESP = 4
    HEARTBEAT = 5
    NACK = 6
    NOT_AVAILABLE = 7


DATA_TYPES = frozenset({DRAW, CLEAR})

# DRAW payload: x h, y h, char c, colour B.
_DRAW_FORMAT = "!hhcB"
_DRAW_SIZE = struct.calcsize(_DRAW_FORMAT)  # 6

_STATE_REQ_FORMAT = "!I"
_STATE_REQ_SIZE = struct.calcsize(_STATE_REQ_FORMAT)  # 4

CHUNK_HEADER_FORMAT = "!IHHIHHII"
CHUNK_HEADER_SIZE = struct.calcsize(CHUNK_HEADER_FORMAT)  # 24
CELL_RECORD_FORMAT = "!hhcBII"
CELL_RECORD_SIZE = struct.calcsize(CELL_RECORD_FORMAT)  # 14
MAX_CELLS_PER_CHUNK = 80  # 20 + 24 + 80 * 14 = 1,164-byte datagram

_NACK_FORMAT = "!IIH"
_NOT_AVAILABLE_FORMAT = "!IHI"
_RANGE_SIZE = struct.calcsize(_NACK_FORMAT)  # 10, the same for NOT_AVAILABLE

# Codes whose payloads this version does not handle; rejected like unknown
# types. Every code is implemented now.
_UNIMPLEMENTED: frozenset = frozenset()


def new_peer_id() -> int:
    """A random non-zero 32-bit peer ID (0 means "no peer" in canvas stamps)."""
    return secrets.randbelow(MAX_U32) + 1


def check_int(name: str, value, maximum: int, minimum: int = 0) -> int:
    # bool is a subclass of int but is never a meaningful field value here.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"{name} must be an integer, got {value!r}")
    if not minimum <= value <= maximum:
        raise ProtocolError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _check_char(value) -> str:
    if not isinstance(value, str) or len(value) != 1 or not " " <= value <= "~":
        raise ProtocolError(f"char must be one printable ASCII character, got {value!r}")
    return value


def _message_type(name) -> MessageType:
    try:
        message_type = MessageType[name]
    except (KeyError, TypeError):
        raise ProtocolError(f"unknown message type {name!r}") from None
    if message_type in _UNIMPLEMENTED:
        raise ProtocolError(f"message type {message_type.name} is not implemented")
    return message_type


# -- payloads -------------------------------------------------------------


def _encode_cell(cell) -> bytes:
    if not isinstance(cell, (tuple, list)) or len(cell) != 6:
        raise ProtocolError(f"cell record must be (x, y, char, color, lamport, peer), got {cell!r}")
    x, y, char, color, lamport, peer = cell
    return struct.pack(
        CELL_RECORD_FORMAT,
        check_int("x", x, MAX_COORDINATE), check_int("y", y, MAX_COORDINATE),
        _check_char(char).encode("ascii"), check_int("color", color, MAX_COLOR),
        check_int("lamport", lamport, MAX_U32), check_int("peer", peer, MAX_U32))


def _decode_cell(record: bytes) -> tuple:
    x, y, char, color, lamport, peer = struct.unpack(CELL_RECORD_FORMAT, record)
    check_int("x", x, MAX_COORDINATE)
    check_int("y", y, MAX_COORDINATE)
    return (x, y, _check_char(char.decode("latin-1")), color, lamport, peer)


def _encode_state_resp(message: dict) -> bytes:
    cells = message["cells"]
    if not isinstance(cells, (list, tuple)) or len(cells) > MAX_CELLS_PER_CHUNK:
        raise ProtocolError(f"cells must be a list of at most {MAX_CELLS_PER_CHUNK} records")
    count = check_int("count", message["count"], MAX_U16, minimum=1)
    index = check_int("index", message["index"], count - 1)
    clear_lamport, clear_peer = message["clear_stamp"]
    header = struct.pack(
        CHUNK_HEADER_FORMAT,
        check_int("nonce", message["nonce"], MAX_U32), index, count,
        check_int("crc", message["crc"], MAX_U32),
        check_int("width", message["width"], MAX_COORDINATE + 1, minimum=1),
        check_int("height", message["height"], MAX_COORDINATE + 1, minimum=1),
        check_int("clear lamport", clear_lamport, MAX_U32),
        check_int("clear peer", clear_peer, MAX_U32))
    return header + b"".join(_encode_cell(cell) for cell in cells)


def _decode_state_resp(payload: bytes) -> dict:
    if len(payload) < CHUNK_HEADER_SIZE:
        raise ProtocolError(f"STATE_RESP payload shorter than {CHUNK_HEADER_SIZE} bytes")
    records = payload[CHUNK_HEADER_SIZE:]
    if len(records) % CELL_RECORD_SIZE:
        raise ProtocolError(f"STATE_RESP records are not a multiple of {CELL_RECORD_SIZE} bytes")
    if len(records) // CELL_RECORD_SIZE > MAX_CELLS_PER_CHUNK:
        raise ProtocolError(f"STATE_RESP carries more than {MAX_CELLS_PER_CHUNK} cells")
    (nonce, index, count, crc, width, height,
     clear_lamport, clear_peer) = struct.unpack_from(CHUNK_HEADER_FORMAT, payload)
    if count == 0 or index >= count:
        raise ProtocolError(f"chunk index {index} is not below chunk count {count}")
    for name, value in (("width", width), ("height", height)):
        check_int(name, value, MAX_COORDINATE + 1, minimum=1)
    return {
        "nonce": nonce, "index": index, "count": count, "crc": crc,
        "width": width, "height": height, "clear_stamp": (clear_lamport, clear_peer),
        "cells": [_decode_cell(records[i:i + CELL_RECORD_SIZE])
                  for i in range(0, len(records), CELL_RECORD_SIZE)],
    }


def _encode_payload(message_type: MessageType, message: dict) -> bytes:
    if message_type is MessageType.DRAW:
        return struct.pack(
            _DRAW_FORMAT,
            check_int("x", message["x"], MAX_COORDINATE),
            check_int("y", message["y"], MAX_COORDINATE),
            _check_char(message["char"]).encode("ascii"),
            check_int("color", message["color"], MAX_COLOR),
        )
    if message_type is MessageType.STATE_REQ:
        return struct.pack(_STATE_REQ_FORMAT, check_int("nonce", message["nonce"], MAX_U32))
    if message_type is MessageType.STATE_RESP:
        return _encode_state_resp(message)
    if message_type is MessageType.NACK:
        return struct.pack(_NACK_FORMAT,
                           check_int("target", message["target"], MAX_U32, minimum=1),
                           check_int("first", message["first"], MAX_U32),
                           check_int("count", message["count"], MAX_U16, minimum=1))
    if message_type is MessageType.NOT_AVAILABLE:
        return struct.pack(_NOT_AVAILABLE_FORMAT,
                           check_int("first", message["first"], MAX_U32),
                           check_int("count", message["count"], MAX_U16, minimum=1),
                           check_int("oldest", message["oldest"], MAX_U32))
    return b""  # CLEAR, HEARTBEAT


def _decode_payload(message_type: MessageType, payload: bytes) -> dict:
    if message_type is MessageType.DRAW:
        if len(payload) != _DRAW_SIZE:
            raise ProtocolError(f"DRAW payload must be {_DRAW_SIZE} bytes, got {len(payload)}")
        x, y, char, color = struct.unpack(_DRAW_FORMAT, payload)
        check_int("x", x, MAX_COORDINATE)  # rejects negative coordinates
        check_int("y", y, MAX_COORDINATE)
        return {"x": x, "y": y, "char": _check_char(char.decode("latin-1")), "color": color}
    if message_type is MessageType.STATE_REQ:
        if len(payload) != _STATE_REQ_SIZE:
            raise ProtocolError(f"STATE_REQ payload must be {_STATE_REQ_SIZE} bytes")
        return {"nonce": struct.unpack(_STATE_REQ_FORMAT, payload)[0]}
    if message_type is MessageType.STATE_RESP:
        return _decode_state_resp(payload)
    if message_type in (MessageType.NACK, MessageType.NOT_AVAILABLE):
        if len(payload) != _RANGE_SIZE:
            raise ProtocolError(f"{message_type.name} payload must be {_RANGE_SIZE} bytes")
        if message_type is MessageType.NACK:
            target, first, count = struct.unpack(_NACK_FORMAT, payload)
            if target == 0:
                raise ProtocolError("NACK target peer ID 0 is invalid")
            fields = {"target": target, "first": first, "count": count}
        else:
            first, count, oldest = struct.unpack(_NOT_AVAILABLE_FORMAT, payload)
            fields = {"first": first, "count": count, "oldest": oldest}
        if count == 0:
            raise ProtocolError(f"{message_type.name} count must be at least 1")
        return fields
    if payload:
        raise ProtocolError(f"{message_type.name} payload must be empty, got {len(payload)} bytes")
    return {}


# -- frames ---------------------------------------------------------------


def encode_message(message: dict, key: Optional["auth.AuthKey"] = None) -> bytes:
    """Serialize a message dict into one v2 datagram.

    Needs "type", "peer" and "seq" plus the type's payload fields;
    "lamport" defaults to 0 and "retransmit" to False. Other keys are
    ignored. With `key` the AUTH flag is set and an HMAC tag appended.
    Raises ProtocolError on invalid input.
    """
    if not isinstance(message, dict):
        raise ProtocolError(f"message must be a dict, got {type(message).__name__}")
    try:
        message_type = _message_type(message["type"])
        peer = check_int("peer", message["peer"], MAX_U32, minimum=1)
        seq = check_int("seq", message["seq"], MAX_U32)
        lamport = check_int("lamport", message.get("lamport", 0), MAX_U32)
        payload = _encode_payload(message_type, message)
    except KeyError as exc:
        raise ProtocolError(f"message is missing field {exc.args[0]!r}") from None
    flags = FLAG_RETRANSMIT if message.get("retransmit") else 0
    if key is not None:
        flags |= FLAG_AUTH
    header = struct.pack(HEADER_FORMAT, MAGIC, VERSION, message_type, flags, 0,
                         peer, seq, lamport, len(payload))
    frame = header + payload
    return frame + key.tag(frame) if key is not None else frame


def decode_message(data: bytes, key: Optional["auth.AuthKey"] = None) -> dict:
    """Parse one v2 datagram into a message dict.

    With `key`, only frames carrying a valid HMAC tag are accepted; the tag
    is checked before any other field is trusted. Without `key`, tagged
    frames are rejected. Raises LegacyFrameError for a v1 frame,
    AuthenticationError for an authentication failure, and ProtocolError
    for anything else that is not a valid v2 message.
    """
    if len(data) == protocol.FRAME_SIZE and data[:2] == MAGIC.to_bytes(2, "big"):
        raise LegacyFrameError("v1 frame (13 bytes) received in v2 mode")
    if len(data) < HEADER_SIZE:
        raise ProtocolError(f"datagram shorter than the {HEADER_SIZE}-byte v2 header")
    (magic, version, raw_type, flags, reserved,
     peer, seq, lamport, length) = struct.unpack_from(HEADER_FORMAT, data)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic 0x{magic:04X}")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    if flags & ~KNOWN_FLAGS:
        raise ProtocolError(f"unknown flags 0x{flags:02X}")
    tag_size = 0
    if key is None:
        if flags & FLAG_AUTH:
            raise AuthenticationError(
                "authenticated frame, but no authentication key is configured")
    else:
        if not flags & FLAG_AUTH:
            raise AuthenticationError("frame has no authentication tag")
        tag_size = auth.TAG_SIZE
        if length > MAX_PAYLOAD or len(data) != HEADER_SIZE + length + tag_size:
            raise AuthenticationError("invalid authentication tag length")
        if not key.verify(data[:-tag_size], data[-tag_size:]):
            raise AuthenticationError("authentication tag does not match")
        data = data[:-tag_size]
    if reserved:
        raise ProtocolError("reserved header byte must be 0")
    if peer == 0:
        raise ProtocolError("peer ID 0 is invalid")
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"payload length {length} exceeds {MAX_PAYLOAD}")
    if len(data) != HEADER_SIZE + length:
        raise ProtocolError(
            f"payload length field is {length} but {len(data) - HEADER_SIZE} bytes follow")
    try:
        message_type = MessageType(raw_type)
    except ValueError:
        raise ProtocolError(f"unknown message type {raw_type}") from None
    if message_type in _UNIMPLEMENTED:
        raise ProtocolError(f"message type {message_type.name} is not implemented")

    message = {
        "type": message_type.name,
        "peer": peer,
        "seq": seq,
        "lamport": lamport,
        "retransmit": bool(flags & FLAG_RETRANSMIT),
    }
    if key is not None:
        message["authenticated"] = True
    message.update(_decode_payload(message_type, data[HEADER_SIZE:]))
    return message
