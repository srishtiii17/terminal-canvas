import struct
import unittest
from unittest import mock

import protocol
import protocol_v2
from protocol import ProtocolError
from protocol_v2 import (
    HEADER_FORMAT, HEADER_SIZE, LegacyFrameError, MessageType, decode_message,
    encode_message,
)

PEER = 0x1A2B3C4D


def draw(**overrides):
    message = {"type": "DRAW", "peer": PEER, "seq": 7, "lamport": 0,
               "retransmit": False, "x": 20, "y": 10, "char": "#", "color": 2}
    message.update(overrides)
    return message


def clear(**overrides):
    message = {"type": "CLEAR", "peer": PEER, "seq": 8, "lamport": 0, "retransmit": False}
    message.update(overrides)
    return message


def header(type_=1, flags=0, reserved=0, peer=PEER, seq=0, lamport=0, length=6,
           magic=protocol_v2.MAGIC, version=2):
    return struct.pack(HEADER_FORMAT, magic, version, type_, flags, reserved,
                       peer, seq, lamport, length)


DRAW_PAYLOAD = struct.pack("!hhcB", 1, 2, b"#", 3)


class HeaderLayoutTests(unittest.TestCase):
    def test_header_is_20_bytes(self):
        self.assertEqual(HEADER_SIZE, 20)
        self.assertEqual(struct.calcsize(HEADER_FORMAT), 2 + 1 + 1 + 1 + 1 + 4 + 4 + 4 + 2)

    def test_draw_exact_bytes(self):
        data = encode_message(draw(seq=0x01020304, lamport=0x0A0B0C0D, x=20, y=10,
                                   char="#", color=7))
        self.assertEqual(
            data,
            b"\xca\x55"            # magic
            + b"\x02"              # version
            + b"\x01"              # type DRAW
            + b"\x00"              # flags
            + b"\x00"              # reserved
            + b"\x1a\x2b\x3c\x4d"  # peer ID
            + b"\x01\x02\x03\x04"  # sequence
            + b"\x0a\x0b\x0c\x0d"  # lamport
            + b"\x00\x06"          # payload length
            + b"\x00\x14\x00\x0a#\x07",  # x=20, y=10, '#', colour 7
        )
        self.assertEqual(len(data), 26)

    def test_clear_exact_bytes(self):
        self.assertEqual(
            encode_message(clear(seq=5)),
            b"\xca\x55\x02\x02\x00\x00\x1a\x2b\x3c\x4d\x00\x00\x00\x05"
            b"\x00\x00\x00\x00\x00\x00")

    def test_retransmit_flag(self):
        data = encode_message(draw(retransmit=True))
        self.assertEqual(data[4], protocol_v2.FLAG_RETRANSMIT)
        self.assertTrue(decode_message(data)["retransmit"])

    def test_type_codes_extend_v1(self):
        self.assertEqual({t.name: t.value for t in MessageType}, {
            "DRAW": 1, "CLEAR": 2, "STATE_REQ": 3, "STATE_RESP": 4,
            "HEARTBEAT": 5, "NACK": 6, "NOT_AVAILABLE": 7})
        for name in ("DRAW", "CLEAR", "STATE_REQ", "STATE_RESP"):
            self.assertEqual(MessageType[name].value, protocol.MessageType[name].value)


class RoundTripTests(unittest.TestCase):
    def test_draw_and_clear(self):
        for message in (draw(), clear(), draw(retransmit=True), clear(lamport=99)):
            with self.subTest(message=message):
                self.assertEqual(decode_message(encode_message(message)), message)

    def test_boundaries(self):
        for fields in ({"x": 0, "y": 0}, {"x": 32767, "y": 32767}, {"color": 255},
                       {"char": " "}, {"char": "~"}, {"seq": 2**32 - 1},
                       {"lamport": 2**32 - 1}, {"peer": 1}, {"peer": 2**32 - 1}):
            with self.subTest(fields=fields):
                message = draw(**fields)
                self.assertEqual(decode_message(encode_message(message)), message)

    def test_defaults_and_extra_keys(self):
        message = {"type": "DRAW", "peer": PEER, "seq": 1, "x": 1, "y": 1,
                   "char": "*", "color": 0, "addr": ("192.0.2.1", 5)}
        decoded = decode_message(encode_message(message))
        self.assertEqual((decoded["lamport"], decoded["retransmit"]), (0, False))
        self.assertNotIn("addr", decoded)

    def test_clear_ignores_draw_fields(self):
        self.assertEqual(len(encode_message(clear(x=5, char="#"))), HEADER_SIZE)


class EncodeValidationTests(unittest.TestCase):
    def test_rejects_non_dict(self):
        with self.assertRaises(ProtocolError):
            encode_message([1, 2])

    def test_rejects_unknown_and_unimplemented_types(self):
        for type_ in ("PAINT", None, 1, "draw", "NACK", "NOT_AVAILABLE"):  # NACKs lack fields.
            with self.subTest(type_=type_), self.assertRaises(ProtocolError):
                encode_message(draw(type=type_))

    def test_rejects_missing_fields(self):
        for field in ("type", "peer", "seq", "x", "y", "char", "color"):
            message = draw()
            del message[field]
            with self.subTest(field=field), self.assertRaisesRegex(ProtocolError, "missing"):
                encode_message(message)

    def test_rejects_invalid_fields(self):
        for fields in ({"peer": 0}, {"peer": 2**32}, {"peer": -1}, {"peer": True},
                       {"seq": -1}, {"seq": 2**32}, {"lamport": -1}, {"lamport": 2**32},
                       {"x": -1}, {"y": 32768}, {"x": 1.0}, {"color": 256},
                       {"char": "ab"}, {"char": "\x00"}, {"char": "é"}):
            with self.subTest(fields=fields), self.assertRaises(ProtocolError):
                encode_message(draw(**fields))


class DecodeValidationTests(unittest.TestCase):
    def test_accepts_handmade_frame(self):
        self.assertEqual(decode_message(header(seq=3) + DRAW_PAYLOAD), {
            "type": "DRAW", "peer": PEER, "seq": 3, "lamport": 0, "retransmit": False,
            "x": 1, "y": 2, "char": "#", "color": 3})

    def test_v1_frame_is_reported_as_legacy(self):
        v1 = protocol.encode_event({"type": "CLEAR", "seq": 0})
        with self.assertRaises(LegacyFrameError):
            decode_message(v1)
        self.assertTrue(issubclass(LegacyFrameError, ProtocolError))

    def test_rejects_short_and_garbage(self):
        for data in (b"", b"\xca\x55", header()[:-1], b"\x00" * 13, b"garbage" * 3):
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                decode_message(data)

    def test_rejects_bad_magic_and_version(self):
        for kwargs, pattern in (({"magic": 0xBEEF}, "magic"), ({"version": 1}, "version"),
                                ({"version": 3}, "version")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ProtocolError, pattern):
                decode_message(header(**kwargs) + DRAW_PAYLOAD)

    def test_rejects_invalid_payload_lengths(self):
        cases = [
            (header(length=5) + DRAW_PAYLOAD, "length field"),
            (header(length=7) + DRAW_PAYLOAD, "length field"),
            (header(length=6) + DRAW_PAYLOAD + b"!", "length field"),
            (header(length=5) + DRAW_PAYLOAD[:5], "DRAW payload must be 6"),
            (header(length=7) + DRAW_PAYLOAD + b"!", "DRAW payload must be 6"),
            (header(type_=2, length=1) + b"!", "CLEAR payload must be empty"),
            (header(length=1201) + b"\x00" * 1201, "exceeds"),
        ]
        for data, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ProtocolError, pattern):
                decode_message(data)

    def test_rejects_invalid_message_types(self):
        for type_ in (0, 8, 255):
            with self.subTest(type_=type_), self.assertRaisesRegex(ProtocolError, "unknown"):
                decode_message(header(type_=type_, length=0))

    def test_later_types_validate_their_payload_length(self):
        # Types 3-7 were rejected as "not implemented" until their phases;
        # each now has a fixed or bounded payload that is checked.
        self.assertEqual(decode_message(header(type_=5, length=0))["type"], "HEARTBEAT")
        for type_, pattern in ((3, "STATE_REQ payload"), (4, "shorter than 24"),
                               (5, "HEARTBEAT payload must be empty"),
                               (6, "NACK payload must be 10"),
                               (7, "NOT_AVAILABLE payload must be 10")):
            length = 1 if type_ == 5 else 0
            with self.subTest(type_=type_), self.assertRaisesRegex(ProtocolError, pattern):
                decode_message(header(type_=type_, length=length) + b"\x00" * length)

    def test_rejects_bad_flags_reserved_and_peer(self):
        cases = [
            ({"flags": 0x04}, "unknown flags"),
            ({"flags": 0x80}, "unknown flags"),
            ({"flags": protocol_v2.FLAG_AUTH}, "authentication"),
            ({"reserved": 1}, "reserved"),
            ({"peer": 0}, "peer ID 0"),
        ]
        for kwargs, pattern in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ProtocolError, pattern):
                decode_message(header(**kwargs) + DRAW_PAYLOAD)

    def test_rejects_invalid_draw_fields(self):
        for payload in (struct.pack("!hhcB", -1, 0, b"#", 0),
                        struct.pack("!hhcB", 0, -5, b"#", 0),
                        struct.pack("!hhcB", 0, 0, b"\x00", 0),
                        struct.pack("!hhcB", 0, 0, b"\x7f", 0),
                        struct.pack("!hhcB", 0, 0, b"\xe9", 0)):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                decode_message(header() + payload)


class PeerIdTests(unittest.TestCase):
    def test_ids_are_32_bit_and_non_zero(self):
        ids = {protocol_v2.new_peer_id() for _ in range(2000)}
        self.assertGreater(len(ids), 1990)  # Collisions are vanishingly rare.
        self.assertTrue(all(1 <= i <= 2**32 - 1 for i in ids))

    def test_uses_secrets_and_never_returns_zero(self):
        with mock.patch.object(protocol_v2.secrets, "randbelow", return_value=0) as rand:
            self.assertEqual(protocol_v2.new_peer_id(), 1)
        rand.assert_called_once_with(2**32 - 1)


class V1UnchangedTests(unittest.TestCase):
    def test_v1_module_is_untouched(self):
        self.assertEqual(protocol.FRAME_FORMAT, "!HBBIhhc")
        self.assertEqual(protocol.FRAME_SIZE, 13)
        self.assertEqual(protocol.RESERVED_TYPES,
                         {protocol.MessageType.STATE_REQ, protocol.MessageType.STATE_RESP})
        with self.assertRaises(ProtocolError):
            protocol.decode_event(encode_message(draw()))  # v2 is never 13 bytes.


if __name__ == "__main__":
    unittest.main()
