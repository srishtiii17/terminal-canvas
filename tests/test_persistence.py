import io
import json
import os
import queue
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import app
import network
import persistence
import protocol
from app import CanvasApp
from canvas import BLANK, DEFAULT_COLOR, Canvas, CanvasError
from persistence import PersistenceError, load_canvas, save_canvas


def sample_canvas():
    canvas = Canvas(10, 5)
    canvas.draw(0, 0, "#", 1)
    canvas.draw(9, 4, "~", 7)
    canvas.draw(3, 2, "@", 255)
    canvas.draw(5, 1, " ", 3)  # A coloured space is still an occupied cell.
    return canvas


def visible(canvas):
    return [(canvas.row_text(y), canvas.row_colors(y)) for y in range(canvas.height)]


def document(cells=(), **overrides):
    doc = {"format": "terminal-canvas", "version": 1, "width": 10, "height": 5,
           "cells": list(cells)}
    doc.update(overrides)
    return doc


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name

    def path(self, name="canvas.json"):
        return os.path.join(self.dir, name)

    def write(self, content, name="canvas.json"):
        path = self.path(name)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as handle:
            handle.write(content if isinstance(content, (str, bytes)) else json.dumps(content))
        return path


# ---------------------------------------------------------------------------
# Canvas.snapshot() / Canvas.restore()
# ---------------------------------------------------------------------------


class SnapshotTests(unittest.TestCase):
    def test_snapshot_lists_only_occupied_cells_in_row_order(self):
        self.assertEqual(sample_canvas().snapshot(), {
            "width": 10, "height": 5,
            "cells": [
                {"x": 0, "y": 0, "char": "#", "color": 1},
                {"x": 5, "y": 1, "char": " ", "color": 3},
                {"x": 3, "y": 2, "char": "@", "color": 255},
                {"x": 9, "y": 4, "char": "~", "color": 7},
            ],
        })

    def test_erased_cell_is_not_stored(self):
        canvas = Canvas(10, 5)
        canvas.draw(1, 1, "#", 2)
        canvas.draw(1, 1, BLANK, DEFAULT_COLOR)
        self.assertEqual(canvas.snapshot()["cells"], [])

    def test_restore_round_trip_and_blanks_unlisted_cells(self):
        target = Canvas(10, 5)
        target.draw(2, 2, "x", 4)  # Not in the snapshot: must be blanked.
        target.restore(sample_canvas().snapshot())
        self.assertEqual(visible(target), visible(sample_canvas()))

    def test_invalid_snapshot_leaves_canvas_unchanged(self):
        canvas = sample_canvas()
        before = visible(canvas)
        bad = canvas.snapshot()
        bad["cells"].append({"x": 1, "y": 1, "char": "\x00", "color": 0})
        with self.assertRaises(CanvasError):
            canvas.restore(bad)
        self.assertEqual(visible(canvas), before)


# ---------------------------------------------------------------------------
# persistence.save_canvas() / load_canvas()
# ---------------------------------------------------------------------------


class SaveLoadTests(TempDirTestCase):
    def test_save_then_load_gives_identical_visible_canvas(self):
        save_canvas(sample_canvas(), self.path())
        loaded = Canvas(10, 5)
        load_canvas(loaded, self.path())
        self.assertEqual(visible(loaded), visible(sample_canvas()))

    def test_full_default_size_canvas_round_trip(self):
        canvas = Canvas()
        for y in range(canvas.height):
            for x in range(canvas.width):
                canvas.draw(x, y, chr(33 + (x + y) % 94), (x * y) % 256)
        save_canvas(canvas, self.path())
        loaded = Canvas()
        load_canvas(loaded, self.path())
        self.assertEqual(visible(loaded), visible(canvas))
        self.assertLess(os.path.getsize(self.path()), persistence.MAX_FILE_BYTES)

    def test_empty_canvas_round_trip(self):
        save_canvas(Canvas(10, 5), self.path())
        with open(self.path()) as handle:
            self.assertEqual(json.load(handle), document())
        loaded = sample_canvas()
        load_canvas(loaded, self.path())
        self.assertEqual(visible(loaded), visible(Canvas(10, 5)))

    def test_file_format(self):
        save_canvas(sample_canvas(), self.path())
        with open(self.path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved, document(sample_canvas().snapshot()["cells"]))

    def test_save_overwrites_existing_file(self):
        save_canvas(sample_canvas(), self.path())
        save_canvas(Canvas(10, 5), self.path())
        loaded = sample_canvas()
        load_canvas(loaded, self.path())
        self.assertEqual(loaded.snapshot()["cells"], [])

    def assert_load_fails(self, content, pattern):
        path = self.write(content)
        canvas = sample_canvas()
        before = visible(canvas)
        with self.assertRaisesRegex(PersistenceError, pattern):
            load_canvas(canvas, path)
        self.assertEqual(visible(canvas), before, "failed load changed the canvas")

    def test_corrupted_json(self):
        good = json.dumps(document([{"x": 0, "y": 0, "char": "#", "color": 1}]))
        for content in (good[:-5], "", "{", "not json", "NaN"):
            with self.subTest(content=content):
                self.assert_load_fails(content, "not valid JSON|not a terminal canvas")

    def test_not_a_canvas_save(self):
        for content in ([], 7, '"text"', {"cells": []}, document(format="other")):
            with self.subTest(content=content):
                self.assert_load_fails(content, "not a terminal canvas save")

    def test_unsupported_version(self):
        for version in (0, 2, "1", 1.5, True, None):
            with self.subTest(version=version):
                self.assert_load_fails(document(version=version), "unsupported file version")
        doc = document()
        del doc["version"]
        self.assert_load_fails(doc, "unsupported file version")

    def test_unexpected_top_level_field(self):
        self.assert_load_fails(document(owner="peer A"), "unexpected fields")

    def test_incorrect_dimensions(self):
        for overrides in ({"width": 60}, {"height": 18}, {"width": "10"},
                          {"width": None}, {"height": 5.0}):
            with self.subTest(overrides=overrides):
                self.assert_load_fails(document(**overrides), "does not match")

    def test_invalid_characters(self):
        for char in ("\x00", "\x7f", "\n", "é", "ab", "", 35, None):
            with self.subTest(char=char):
                cell = {"x": 1, "y": 1, "char": char, "color": 0}
                self.assert_load_fails(document([cell]), "char must be")

    def test_invalid_colors(self):
        for color in (-1, 256, "2", 2.0, True, None):
            with self.subTest(color=color):
                cell = {"x": 1, "y": 1, "char": "#", "color": color}
                self.assert_load_fails(document([cell]), "color must be")

    def test_malformed_cells(self):
        cases = [
            ("not a list", "cells must be a list"),
            ([["x", 1]], "exactly x, y, char and color"),
            ([{"x": 1, "y": 1, "char": "#"}], "exactly x, y, char and color"),
            ([{"x": 1, "y": 1, "char": "#", "color": 0, "stamp": 5}],
             "exactly x, y, char and color"),
            ([{"x": 10, "y": 0, "char": "#", "color": 0}], "outside the canvas"),
            ([{"x": -1, "y": 0, "char": "#", "color": 0}], "outside the canvas"),
            ([{"x": "1", "y": 0, "char": "#", "color": 0}], "outside the canvas"),
            ([{"x": 1, "y": 0, "char": "#", "color": 0},
              {"x": 1, "y": 0, "char": "*", "color": 0}], "repeats"),
        ]
        for cells, pattern in cases:
            with self.subTest(cells=cells):
                doc = document()
                doc["cells"] = cells  # Exactly as given: may not be a list.
                self.assert_load_fails(doc, pattern)

    def test_more_cells_than_the_canvas_holds(self):
        cells = [{"x": 0, "y": 0, "char": "#", "color": 0}] * 51
        self.assert_load_fails(document(cells), "do not fit")

    def test_oversized_file_is_rejected_before_parsing(self):
        content = " " * (persistence.MAX_FILE_BYTES + 1)
        with mock.patch.object(persistence.json, "loads") as parse:
            self.assert_load_fails(content, "larger than")
        parse.assert_not_called()

    def test_non_utf8_file(self):
        self.assert_load_fails(b"\xff\xfe{}", "not UTF-8")

    def test_missing_file(self):
        canvas = sample_canvas()
        with self.assertRaisesRegex(PersistenceError, "cannot read"):
            load_canvas(canvas, self.path("absent.json"))
        self.assertEqual(visible(canvas), visible(sample_canvas()))

    def test_persistence_error_is_value_error(self):
        self.assertTrue(issubclass(PersistenceError, ValueError))


class AtomicWriteTests(TempDirTestCase):
    def test_temp_file_is_complete_and_closed_before_replace(self):
        target = self.path()
        seen = {}
        real_replace = os.replace

        def checking_replace(src, dst):
            seen["src"], seen["dst"] = src, dst
            self.assertEqual(os.path.dirname(os.path.abspath(src)), self.dir)
            self.assertFalse(os.path.exists(dst))
            with open(src, encoding="utf-8") as handle:  # Readable: fully written.
                seen["content"] = json.load(handle)
            real_replace(src, dst)

        with mock.patch.object(persistence.os, "replace", side_effect=checking_replace):
            save_canvas(sample_canvas(), target)
        self.assertEqual(seen["dst"], target)
        self.assertEqual(seen["content"], document(sample_canvas().snapshot()["cells"]))
        self.assertFalse(os.path.exists(seen["src"]))
        self.assertEqual(os.listdir(self.dir), ["canvas.json"])

    def test_data_is_flushed_to_disk(self):
        with mock.patch.object(persistence.os, "fsync", wraps=os.fsync) as fsync:
            save_canvas(sample_canvas(), self.path())
        fsync.assert_called_once()

    def test_failed_replace_keeps_existing_save_and_removes_temp(self):
        save_canvas(sample_canvas(), self.path())
        with open(self.path(), "rb") as handle:
            original = handle.read()
        with mock.patch.object(persistence.os, "replace", side_effect=OSError(13, "denied")):
            with self.assertRaisesRegex(PersistenceError, "cannot write"):
                save_canvas(Canvas(10, 5), self.path())
        with open(self.path(), "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(os.listdir(self.dir), ["canvas.json"])

    def test_failure_while_writing_keeps_existing_save(self):
        save_canvas(sample_canvas(), self.path())
        with mock.patch.object(persistence.os, "fsync", side_effect=OSError(28, "disk full")):
            with self.assertRaises(PersistenceError):
                save_canvas(Canvas(10, 5), self.path())
        loaded = Canvas(10, 5)
        load_canvas(loaded, self.path())
        self.assertEqual(visible(loaded), visible(sample_canvas()))
        self.assertEqual(os.listdir(self.dir), ["canvas.json"])

    def test_missing_directory(self):
        with self.assertRaisesRegex(PersistenceError, "cannot write"):
            save_canvas(sample_canvas(), os.path.join(self.dir, "no", "such", "x.json"))


class ProtocolUnchangedTests(unittest.TestCase):
    """Persistence is local: it must leave the v1 wire format alone."""

    def test_v1_frame_is_unchanged(self):
        self.assertEqual(protocol.FRAME_FORMAT, "!HBBIhhc")
        self.assertEqual(protocol.FRAME_SIZE, 13)
        self.assertEqual(protocol.MAGIC, 0xCA55)
        self.assertEqual(protocol.RESERVED_TYPES,
                         {protocol.MessageType.STATE_REQ, protocol.MessageType.STATE_RESP})

    def test_persistence_does_not_use_the_network(self):
        self.assertFalse(hasattr(persistence, "network"))
        self.assertFalse(hasattr(persistence, "protocol"))


# ---------------------------------------------------------------------------
# Keyboard controls (S and L) in CanvasApp
# ---------------------------------------------------------------------------


class FakeSend:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(dict(event))
        return len(self.events) - 1


class KeyboardTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.send = FakeSend()
        self.inbox = queue.Queue()
        self.app = CanvasApp(Canvas(10, 5), self.send, self.inbox,
                             save_path=self.path(), load_path=self.path())

    def test_default_paths(self):
        application = CanvasApp(Canvas(10, 5), FakeSend(), queue.Queue())
        self.assertEqual(application.save_path, persistence.DEFAULT_FILE)
        self.assertEqual(application.load_path, persistence.DEFAULT_FILE)

    def test_s_saves_and_l_restores_without_sending(self):
        self.app.handle_key(" ")
        self.app.handle_key("S")
        self.assertIn("Saved to", self.app.message)
        sent = list(self.send.events)
        saved = visible(self.app.canvas)

        self.app.handle_key("RIGHT")
        self.app.handle_key(" ")
        self.app.handle_key("L")
        self.assertIn("Loaded", self.app.message)
        self.assertIn("local only", self.app.message)
        self.assertEqual(visible(self.app.canvas), saved)
        self.assertEqual(len(self.send.events), len(sent) + 1)  # Only the second draw.
        self.assertEqual(self.app.sent_count, 2)

    def test_save_and_load_send_nothing(self):
        self.app.handle_key("S")
        self.app.handle_key("L")
        self.assertEqual(self.send.events, [])
        self.assertEqual(self.app.sent_count, 0)
        self.assertTrue(self.app.running)

    def test_failed_load_shows_message_and_keeps_canvas(self):
        self.write("{broken")
        self.app.handle_key(" ")
        before = visible(self.app.canvas)
        self.app.handle_key("L")
        self.assertIn("Load failed", self.app.message)
        self.assertIn("not valid JSON", self.app.message)
        self.assertEqual(visible(self.app.canvas), before)
        self.assertTrue(self.app.running)

    def test_load_of_missing_file_is_reported(self):
        self.app.handle_key("L")
        self.assertIn("Load failed", self.app.message)

    def test_failed_save_is_reported(self):
        self.app.save_path = os.path.join(self.dir, "missing", "x.json")
        self.app.handle_key("S")
        self.assertIn("Save failed", self.app.message)
        self.assertTrue(self.app.running)

    def test_keys_mark_view_dirty(self):
        self.app.dirty = False
        self.app.handle_key("S")
        self.assertTrue(self.app.dirty)

    def test_lowercase_s_and_l_are_not_bound(self):
        for key in ("s", "l"):
            self.app.handle_key(key)
            self.assertIn("Unknown key", self.app.message)
        self.assertFalse(os.path.exists(self.path()))

    def test_brush_selection_takes_s_and_l_literally(self):
        for key in ("S", "L"):
            self.app.handle_key("b")
            self.app.handle_key(key)
            self.assertEqual(self.app.brush, key)
        self.assertFalse(os.path.exists(self.path()))

    def test_help_text_lists_save_and_load_after_existing_keys(self):
        self.assertIn("X clear all  q quit  S save  L load", app.HELP_TEXT)


# ---------------------------------------------------------------------------
# Command-line options (--load / --save) in main()
# ---------------------------------------------------------------------------


class FakeCursesError(Exception):
    pass


def fake_curses():
    c = mock.MagicMock(name="curses")
    c.error = FakeCursesError
    c.KEY_UP, c.KEY_DOWN, c.KEY_LEFT, c.KEY_RIGHT, c.KEY_RESIZE = 259, 258, 260, 261, 410
    c.has_colors.return_value = False
    return c


class CommandLineTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.start = mock.patch.object(network, "start_network").start()
        self.start.return_value = mock.Mock(
            running=True, local_address=("192.0.2.1", 1),
            tracker=mock.Mock(totals=lambda: {"missing": 0, "duplicates": 0}))
        self.stop = mock.patch.object(network, "stop_network").start()
        self.send = mock.patch.object(network, "send_event").start()
        mock.patch.object(app.logging, "basicConfig").start()
        self.addCleanup(mock.patch.stopall)
        self.curses = fake_curses()
        mock.patch.object(app, "load_curses", return_value=self.curses).start()
        self.keys = [ord("q")]
        self.rendered = []

        def fake_wrapper(func):
            scr = mock.MagicMock()
            scr.getmaxyx.return_value = (30, 120)
            scr.getch.side_effect = self.keys
            scr.addnstr.side_effect = lambda y, x, text, n, attr: self.rendered.append(text)
            func(scr)

        self.curses.wrapper.side_effect = fake_wrapper

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = app.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def saved_full_canvas(self, name="in.json"):
        canvas = Canvas()
        canvas.draw(0, 0, "Q", 2)
        canvas.draw(59, 17, "Z", 5)
        save_canvas(canvas, self.path(name))
        return canvas

    def test_options_default_to_none(self):
        args = app.parse_args([])
        self.assertIsNone(args.load)
        self.assertIsNone(args.save)
        args = app.parse_args(["--load", "a.json", "--save", "b.json"])
        self.assertEqual((args.load, args.save), ("a.json", "b.json"))

    def test_no_options_creates_no_file(self):
        cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertNotIn("Saved", out)

    def test_load_option_shows_saved_canvas_and_sends_nothing(self):
        self.saved_full_canvas()
        code, _, _ = self.run_main("--load", self.path("in.json"))
        self.assertEqual(code, 0)
        self.assertTrue(any(text.startswith("Q") for text in self.rendered))
        self.assertTrue(any(text.endswith("Z") for text in self.rendered))
        self.send.assert_not_called()

    def test_bad_load_file_exits_before_joining_the_group(self):
        self.write("{oops", "in.json")
        code, _, err = self.run_main("--load", self.path("in.json"))
        self.assertEqual(code, 1)
        self.assertIn("Could not load canvas", err)
        self.assertIn("not valid JSON", err)
        self.start.assert_not_called()
        self.curses.wrapper.assert_not_called()

    def test_missing_load_file_exits_with_message(self):
        code, _, err = self.run_main("--load", self.path("absent.json"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        self.start.assert_not_called()

    def test_wrong_size_load_file_exits_with_message(self):
        save_canvas(Canvas(10, 5), self.path("in.json"))
        code, _, err = self.run_main("--load", self.path("in.json"))
        self.assertEqual(code, 1)
        self.assertIn("does not match 60x18", err)

    def test_save_option_writes_canvas_on_exit(self):
        self.keys[:0] = [ord(" ")]
        code, out, _ = self.run_main("--save", self.path("out.json"))
        self.assertEqual(code, 0)
        self.assertIn("Saved canvas to", out)
        loaded = Canvas()
        load_canvas(loaded, self.path("out.json"))
        self.assertEqual(loaded.cell(0, 0), ("#", DEFAULT_COLOR))

    def test_save_option_also_saves_after_ctrl_c(self):
        self.curses.wrapper.side_effect = KeyboardInterrupt
        code, _, _ = self.run_main("--save", self.path("out.json"))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.path("out.json")))
        self.stop.assert_called_once()

    def test_save_option_also_saves_after_unexpected_error(self):
        self.curses.wrapper.side_effect = RuntimeError("UI crashed")
        with self.assertRaises(RuntimeError):
            self.run_main("--save", self.path("out.json"))
        self.assertTrue(os.path.exists(self.path("out.json")))
        self.stop.assert_called_once()

    def test_load_and_save_together(self):
        expected = self.saved_full_canvas()
        code, _, _ = self.run_main("--load", self.path("in.json"),
                                   "--save", self.path("out.json"))
        self.assertEqual(code, 0)
        loaded = Canvas()
        load_canvas(loaded, self.path("out.json"))
        self.assertEqual(visible(loaded), visible(expected))

    def test_save_into_missing_folder_exits_before_joining(self):
        code, _, err = self.run_main("--save", os.path.join(self.dir, "nope", "out.json"))
        self.assertEqual(code, 1)
        self.assertIn("folder does not exist", err)
        self.start.assert_not_called()

    def test_failed_save_on_exit_is_reported_not_raised(self):
        with mock.patch.object(persistence.os, "replace", side_effect=OSError(13, "denied")):
            code, _, err = self.run_main("--save", self.path("out.json"))
        self.assertEqual(code, 0)
        self.assertIn("Could not save canvas", err)
        self.stop.assert_called_once()

    def test_keys_use_the_option_paths(self):
        # S with only --load saves back to that file; L reloads it.
        self.saved_full_canvas()
        self.keys[:0] = [ord("e"), ord("S")]  # Erase (0,0), then save.
        self.run_main("--load", self.path("in.json"))
        loaded = Canvas()
        load_canvas(loaded, self.path("in.json"))
        self.assertEqual(loaded.cell(0, 0), (BLANK, DEFAULT_COLOR))
        self.assertEqual(loaded.cell(59, 17), ("Z", 5))


if __name__ == "__main__":
    unittest.main()
