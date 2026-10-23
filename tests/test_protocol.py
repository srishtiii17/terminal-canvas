import struct
import unittest

import protocol
from protocol import (
    CLEAR,
    DRAW,
    FRAME_FORMAT,
    MAGIC,
    MessageType,
    ProtocolError,
    decode_event,
    encode_event,
)


def draw(**overrides):
    event = {"type": DRAW, "seq": 1, "x": 20, "y": 10, "char": "#", "color": 2}
    event.update(overrides)
    return event


def frame(magic=MAGIC, type_=MessageType.DRAW, color=0, seq=0, x=0, y=0, char=b"#"):
    return struct.pack(FRAME_FORMAT, magic, type_, color, seq, x, y, char)


class FrameLayoutTests(unittest.TestCase):
    def test_frame_is_13_bytes(self):
        self.assertEqual(protocol.FRAME_SIZE, 13)
        self.assertEqual(struct.calcsize(FRAME_FORMAT), 2 + 1 + 1 + 4 + 2 + 2 + 1)

    def test_every_encoded_event_is_frame_size(self):
        for event in (draw(), {"type": CLEAR, "seq": 0}):
            self.assertEqual(len(encode_event(event)), protocol.FRAME_SIZE)

    def test_draw_exact_bytes(self):
        data = encode_event(draw(seq=0x01020304, x=20, y=10, char="#", color=7))
        self.assertEqual(
            data,
            b"\xca\x55"  # magic
            + b"\x01"  # type DRAW
            + b"\x07"  # color
            + b"\x01\x02\x03\x04"  # sequence
            + b"\x00\x14"  # x = 20
            + b"\x00\x0a"  # y = 10
            + b"#",  # char
        )

    def test_clear_exact_bytes(self):
        self.assertEqual(
            encode_event({"type": CLEAR, "seq": 5}),
            b"\xca\x55\x02\x00\x00\x00\x00\x05\x00\x00\x00\x00\x00",
        )

    def test_type_codes(self):
        self.assertEqual(
            {t.name: t.value for t in MessageType},
            {"DRAW": 1, "CLEAR": 2, "STATE_REQ": 3, "STATE_RESP": 4},
        )


class RoundTripTests(unittest.TestCase):
    def assert_round_trip(self, event):
        self.assertEqual(decode_event(encode_event(event)), event)

    def test_draw(self):
        self.assert_round_trip(draw())

    def test_draw_boundaries(self):
        self.assert_round_trip(draw(seq=0, x=0, y=0, char=" ", color=0))
        self.assert_round_trip(
            draw(
                seq=protocol.MAX_SEQUENCE,
                x=protocol.MAX_COORDINATE,
                y=protocol.MAX_COORDINATE,
                char="~",
                color=protocol.MAX_COLOR,
            )
        )

    def test_every_printable_character(self):
        for code in range(0x20, 0x7F):
            self.assert_round_trip(draw(char=chr(code)))

    def test_clear(self):
        self.assert_round_trip({"type": CLEAR, "seq": 99})

    def test_extra_keys_are_ignored(self):
        event = draw(addr=("10.0.0.1", 5005))
        self.assertEqual(decode_event(encode_event(event)), draw())

    def test_clear_ignores_draw_fields(self):
        event = {"type": CLEAR, "seq": 3, "x": 9, "y": 9, "char": "x", "color": 9}
        self.assertEqual(decode_event(encode_event(event)), {"type": CLEAR, "seq": 3})


class EncodeValidationTests(unittest.TestCase):
    def test_rejects_non_dict(self):
        for value in (None, "DRAW", [DRAW], b"\x00" * 13):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                encode_event(value)

    def test_rejects_unknown_type(self):
        for type_ in ("PAINT", "draw", 1, None):
            with self.subTest(type_=type_), self.assertRaises(ProtocolError):
                encode_event(draw(type=type_))

    def test_rejects_reserved_types(self):
        for type_ in ("STATE_REQ", "STATE_RESP"):
            with self.subTest(type_=type_):
                with self.assertRaisesRegex(ProtocolError, "not implemented"):
                    encode_event({"type": type_, "seq": 0})

    def test_rejects_missing_fields(self):
        for field in ("type", "seq", "x", "y", "char", "color"):
            event = draw()
            del event[field]
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                encode_event(event)
        with self.assertRaises(ProtocolError):
            encode_event({"type": CLEAR})

    def test_rejects_invalid_draw_fields(self):
        invalid = [
            dict(seq=-1),
            dict(seq=protocol.MAX_SEQUENCE + 1),
            dict(x=-1),
            dict(x=protocol.MAX_COORDINATE + 1),
            dict(y=-1),
            dict(y=protocol.MAX_COORDINATE + 1),
            dict(color=-1),
            dict(color=256),
            dict(x=1.5),
            dict(x="1"),
            dict(color=True),
            dict(char=""),
            dict(char="ab"),
            dict(char="\n"),
            dict(char="\x7f"),
            dict(char="é"),
            dict(char=b"#"),
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ProtocolError):
                encode_event(draw(**overrides))

    def test_protocol_error_is_value_error(self):
        self.assertTrue(issubclass(ProtocolError, ValueError))


class DecodeValidationTests(unittest.TestCase):
    def test_accepts_valid_frame(self):
        event = decode_event(frame(color=3, seq=4, x=5, y=6, char=b"*"))
        self.assertEqual(
            event,
            {"type": DRAW, "seq": 4, "x": 5, "y": 6, "char": "*", "color": 3},
        )

    def test_rejects_wrong_sizes(self):
        valid = frame()
        for data in (b"", valid[:-1], valid + b"\x00", valid * 2):
            with self.subTest(size=len(data)), self.assertRaises(ProtocolError):
                decode_event(data)

    def test_rejects_bad_magic(self):
        for magic in (0x0000, 0x55CA, 0xCA54):
            with self.subTest(magic=hex(magic)), self.assertRaises(ProtocolError):
                decode_event(frame(magic=magic))

    def test_rejects_unknown_type(self):
        for type_ in (0, 5, 255):
            with self.subTest(type_=type_), self.assertRaises(ProtocolError):
                decode_event(frame(type_=type_))

    def test_rejects_reserved_types(self):
        for type_ in (MessageType.STATE_REQ, MessageType.STATE_RESP):
            with self.subTest(type_=type_):
                with self.assertRaisesRegex(ProtocolError, "not implemented"):
                    decode_event(frame(type_=type_))

    def test_rejects_negative_coordinates(self):
        for coords in ({"x": -1}, {"y": -1}, {"x": -32768}):
            with self.subTest(coords=coords), self.assertRaises(ProtocolError):
                decode_event(frame(**coords))

    def test_rejects_non_printable_char(self):
        for char in (b"\x00", b"\x07", b"\x7f", b"\xff"):
            with self.subTest(char=char), self.assertRaises(ProtocolError):
                decode_event(frame(char=char))

    def test_rejects_clear_with_nonzero_fields(self):
        for fields in ({"x": 1}, {"y": 1}, {"color": 1}, {"char": b"#"}):
            data = frame(type_=MessageType.CLEAR, **{"char": b"\x00", **fields})
            with self.subTest(fields=fields), self.assertRaises(ProtocolError):
                decode_event(data)


if __name__ == "__main__":
    unittest.main()
