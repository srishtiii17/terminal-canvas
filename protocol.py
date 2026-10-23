"""Binary wire protocol for terminal canvas events.

Every datagram is exactly one fixed-size 13-byte frame. All integers are
big-endian (network byte order); struct format ``!HBBIhhc``::

    offset  size  field     struct  meaning
    0       2     magic     H       0xCA55, identifies this application's frames
    2       1     type      B       MessageType code
    3       1     color     B       terminal color index, 0-255
    4       4     sequence  I       per-sender counter, wraps at 2**32
    8       2     x         h       canvas column, 0-32767
    10      2     y         h       canvas row, 0-32767
    12      1     char      c       printable ASCII character (0x20-0x7E)

The project presentation describes this as a "12-byte" frame, but the listed
fields (2+1+1+4+2+2+1) and its struct format both total 13 bytes. All fields
are kept, so the frame is 13 bytes.

The magic number only lets a receiver discard unrelated traffic; it provides
no authentication.

Events are plain dicts so the canvas and UI layers need no protocol classes:

    {"type": "DRAW",  "seq": 7, "x": 20, "y": 10, "char": "#", "color": 2}
    {"type": "CLEAR", "seq": 8}

A CLEAR frame carries zeros in color, x and y and a NUL char byte.
"""

from __future__ import annotations

import enum
import struct

MAGIC = 0xCA55

FRAME_FORMAT = "!HBBIhhc"
FRAME_SIZE = struct.calcsize(FRAME_FORMAT)  # 13

MAX_SEQUENCE = 2**32 - 1
MAX_COORDINATE = 2**15 - 1  # x and y are signed 16-bit on the wire
MAX_COLOR = 2**8 - 1

DRAW = "DRAW"
CLEAR = "CLEAR"


class ProtocolError(ValueError):
    """Raised when an event cannot be encoded or a frame cannot be decoded."""


class MessageType(enum.IntEnum):
    DRAW = 1
    CLEAR = 2
    # Reserved for late-join synchronization; not implemented yet.
    STATE_REQ = 3
    STATE_RESP = 4


RESERVED_TYPES = frozenset({MessageType.STATE_REQ, MessageType.STATE_RESP})


def _check_int(name: str, value, maximum: int) -> int:
    # bool is a subclass of int but is never a meaningful field value here.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"{name} must be an integer, got {value!r}")
    if not 0 <= value <= maximum:
        raise ProtocolError(f"{name} must be between 0 and {maximum}, got {value}")
    return value


def _check_char(value) -> str:
    if not isinstance(value, str) or len(value) != 1:
        raise ProtocolError(f"char must be a single character, got {value!r}")
    if not " " <= value <= "~":
        raise ProtocolError(f"char must be printable ASCII (0x20-0x7E), got {value!r}")
    return value


def _message_type(name) -> MessageType:
    try:
        message_type = MessageType[name]
    except (KeyError, TypeError):
        raise ProtocolError(f"unknown event type {name!r}") from None
    if message_type in RESERVED_TYPES:
        raise ProtocolError(f"event type {name} is reserved and not implemented")
    return message_type


def encode_event(event: dict) -> bytes:
    """Serialize an event dict into a 13-byte frame.

    The dict needs "type" and "seq"; DRAW also needs "x", "y", "char" and
    "color". Other keys are ignored. Raises ProtocolError on invalid input.
    """
    if not isinstance(event, dict):
        raise ProtocolError(f"event must be a dict, got {type(event).__name__}")
    try:
        message_type = _message_type(event["type"])
        seq = _check_int("seq", event["seq"], MAX_SEQUENCE)
        if message_type is MessageType.DRAW:
            x = _check_int("x", event["x"], MAX_COORDINATE)
            y = _check_int("y", event["y"], MAX_COORDINATE)
            color = _check_int("color", event["color"], MAX_COLOR)
            char = _check_char(event["char"]).encode("ascii")
        else:
            x = y = color = 0
            char = b"\x00"
    except KeyError as exc:
        raise ProtocolError(f"event is missing field {exc.args[0]!r}") from None
    return struct.pack(FRAME_FORMAT, MAGIC, message_type, color, seq, x, y, char)


def decode_event(packet: bytes) -> dict:
    """Parse a 13-byte frame into an event dict.

    Raises ProtocolError for a wrong size, bad magic, unknown or reserved
    type, or invalid field values.
    """
    if len(packet) != FRAME_SIZE:
        raise ProtocolError(f"frame must be {FRAME_SIZE} bytes, got {len(packet)}")

    magic, raw_type, color, seq, x, y, char = struct.unpack(FRAME_FORMAT, packet)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic 0x{magic:04X}")
    try:
        message_type = MessageType(raw_type)
    except ValueError:
        raise ProtocolError(f"unknown message type {raw_type}") from None
    if message_type in RESERVED_TYPES:
        raise ProtocolError(f"message type {message_type.name} is not implemented")

    if message_type is MessageType.CLEAR:
        if (color, x, y, char) != (0, 0, 0, b"\x00"):
            raise ProtocolError("CLEAR frame must have zero color, x, y and char")
        return {"type": CLEAR, "seq": seq}

    _check_int("x", x, MAX_COORDINATE)  # rejects negative coordinates
    _check_int("y", y, MAX_COORDINATE)
    return {
        "type": DRAW,
        "seq": seq,
        "x": x,
        "y": y,
        "char": _check_char(char.decode("latin-1")),
        "color": color,
    }
