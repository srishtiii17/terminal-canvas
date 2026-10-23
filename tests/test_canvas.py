import unittest

import config
import protocol
from canvas import BLANK, DEFAULT_COLOR, Canvas, CanvasError


def draw(**overrides):
    event = {"type": protocol.DRAW, "x": 3, "y": 2, "char": "#", "color": 4}
    event.update(overrides)
    return event


class InitializationTests(unittest.TestCase):
    def test_default_size_comes_from_config(self):
        canvas = Canvas()
        self.assertEqual((canvas.width, canvas.height),
                         (config.CANVAS_WIDTH, config.CANVAS_HEIGHT))

    def test_starts_blank(self):
        canvas = Canvas(5, 3)
        for y in range(3):
            self.assertEqual(canvas.row_text(y), BLANK * 5)
            self.assertEqual(canvas.row_colors(y), [DEFAULT_COLOR] * 5)

    def test_rejects_invalid_dimensions(self):
        for width, height in ((0, 5), (5, 0), (-1, 5), (32769, 5), (5.0, 5), (True, 5)):
            with self.subTest(size=(width, height)), self.assertRaises(CanvasError):
                Canvas(width, height)

    def test_accepts_largest_protocol_addressable_size(self):
        canvas = Canvas(protocol.MAX_COORDINATE + 1, 1)
        self.assertTrue(canvas.draw(protocol.MAX_COORDINATE, 0, "x"))


class BoundsTests(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas(4, 3)

    def test_in_bounds(self):
        self.assertTrue(self.canvas.in_bounds(0, 0))
        self.assertTrue(self.canvas.in_bounds(3, 2))
        for x, y in ((-1, 0), (0, -1), (4, 0), (0, 3), (1.0, 1), (True, 1), ("1", 1)):
            with self.subTest(xy=(x, y)):
                self.assertFalse(self.canvas.in_bounds(x, y))

    def test_draw_outside_is_ignored_without_change(self):
        for x, y in ((4, 0), (0, 3), (-1, 0), (100, 100)):
            with self.subTest(xy=(x, y)):
                self.assertFalse(self.canvas.draw(x, y, "x"))
        self.assertEqual([self.canvas.row_text(y) for y in range(3)], ["    "] * 3)

    def test_cell_outside_raises(self):
        with self.assertRaises(CanvasError):
            self.canvas.cell(4, 0)


class DrawAndClearTests(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas(4, 3)

    def test_draw_sets_char_and_color(self):
        self.assertTrue(self.canvas.draw(1, 2, "@", 5))
        self.assertEqual(self.canvas.cell(1, 2), ("@", 5))
        self.assertEqual(self.canvas.row_text(2), " @  ")
        self.assertEqual(self.canvas.row_colors(2), [0, 5, 0, 0])

    def test_draw_corners(self):
        for x, y in ((0, 0), (3, 0), (0, 2), (3, 2)):
            self.assertTrue(self.canvas.draw(x, y, "+"))
        self.assertEqual(self.canvas.row_text(0), "+  +")
        self.assertEqual(self.canvas.row_text(2), "+  +")

    def test_later_draw_overwrites(self):
        self.canvas.draw(0, 0, "a", 1)
        self.canvas.draw(0, 0, "b", 2)
        self.assertEqual(self.canvas.cell(0, 0), ("b", 2))

    def test_clear_resets_every_cell(self):
        self.canvas.draw(0, 0, "a", 1)
        self.canvas.draw(3, 2, "b", 7)
        self.canvas.clear()
        for y in range(3):
            self.assertEqual(self.canvas.row_text(y), "    ")
            self.assertEqual(self.canvas.row_colors(y), [0, 0, 0, 0])

    def test_rejects_invalid_char_and_color(self):
        invalid = [
            dict(char=""), dict(char="ab"), dict(char="\n"), dict(char="\x7f"),
            dict(char="é"), dict(char=b"#"), dict(char=None),
            dict(color=-1), dict(color=256), dict(color=1.5), dict(color=True),
        ]
        for overrides in invalid:
            args = {"x": 0, "y": 0, "char": "#", "color": 0, **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(CanvasError):
                self.canvas.draw(**args)
        self.assertEqual(self.canvas.cell(0, 0), (BLANK, DEFAULT_COLOR))

    def test_rejects_non_integer_coordinates(self):
        for x, y in ((1.0, 0), ("1", 0), (None, 0), (0, False)):
            with self.subTest(xy=(x, y)), self.assertRaises(CanvasError):
                self.canvas.draw(x, y, "#")


class ApplyEventTests(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas(10, 5)

    def test_draw_event(self):
        self.assertTrue(self.canvas.apply_event(draw()))
        self.assertEqual(self.canvas.cell(3, 2), ("#", 4))

    def test_decoded_network_event_applies(self):
        # Events from the network carry "seq" and "addr"; they are ignored.
        frame = protocol.encode_event({**draw(), "seq": 9})
        event = {**protocol.decode_event(frame), "addr": ("192.0.2.1", 5000)}
        self.assertTrue(self.canvas.apply_event(event))
        self.assertEqual(self.canvas.cell(3, 2), ("#", 4))

    def test_clear_event(self):
        self.canvas.apply_event(draw())
        self.assertTrue(self.canvas.apply_event({"type": protocol.CLEAR, "seq": 1}))
        self.assertEqual(self.canvas.cell(3, 2), (BLANK, DEFAULT_COLOR))

    def test_out_of_bounds_draw_event_is_ignored(self):
        self.assertFalse(self.canvas.apply_event(draw(x=10)))
        self.assertFalse(self.canvas.apply_event(draw(y=5)))

    def test_malformed_events_raise(self):
        malformed = [
            None, "DRAW", [], {"x": 1}, {"type": "PAINT"}, {"type": "STATE_REQ"},
            {"type": 1, "x": 0, "y": 0, "char": "#", "color": 0},
            {"type": protocol.DRAW, "y": 0, "char": "#", "color": 0},
            {"type": protocol.DRAW, "x": 0, "y": 0, "color": 0},
            {"type": protocol.DRAW, "x": 0, "y": 0, "char": "#"},
            draw(char="\x00"), draw(color=999), draw(x="3"),
        ]
        for event in malformed:
            with self.subTest(event=event), self.assertRaises(CanvasError):
                self.canvas.apply_event(event)
        self.assertEqual(self.canvas.cell(3, 2), (BLANK, DEFAULT_COLOR))

    def test_canvas_error_is_value_error(self):
        self.assertTrue(issubclass(CanvasError, ValueError))


if __name__ == "__main__":
    unittest.main()
