import io
import queue
import random
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import app
import network
import protocol
from app import CanvasApp, CursesView, run_loop
from canvas import BLANK, Canvas


class FakeSend:
    """Stands in for network.send_event; records events, can fail on demand."""

    def __init__(self):
        self.events = []
        self.fail_with = None

    def __call__(self, event):
        if self.fail_with is not None:
            raise self.fail_with
        self.events.append(dict(event))
        return len(self.events) - 1


def make_app(width=10, height=5, **kwargs):
    send = FakeSend()
    inbox = queue.Queue()
    return CanvasApp(Canvas(width, height), send, inbox, **kwargs), send, inbox


def press(application, *keys):
    for key in keys:
        application.handle_key(key)


# ---------------------------------------------------------------------------
# Application logic: no terminal, no sockets.
# ---------------------------------------------------------------------------


class LocalDrawingTests(unittest.TestCase):
    def setUp(self):
        self.app, self.send, self.inbox = make_app()

    def test_space_applies_locally_once_and_sends_once(self):
        with mock.patch.object(self.app.canvas, "apply_event",
                               wraps=self.app.canvas.apply_event) as apply:
            press(self.app, " ")
        self.assertEqual(apply.call_count, 1)
        self.assertEqual(self.app.canvas.cell(0, 0), ("#", 0))
        self.assertEqual(self.send.events, [
            {"type": protocol.DRAW, "x": 0, "y": 0, "char": "#", "color": 0}])
        self.assertTrue(self.inbox.empty())  # Nothing echoed back to apply again.
        self.assertEqual(self.app.sent_count, 1)

    def test_every_sent_event_is_valid_on_the_wire(self):
        press(self.app, "3", "b", "@", "RIGHT", " ", "e", "X")
        for event in self.send.events:
            protocol.encode_event({**event, "seq": 0})  # Raises if invalid.

    def test_movement_is_clamped_to_canvas(self):
        press(self.app, "LEFT", "UP")
        self.assertEqual((self.app.cursor_x, self.app.cursor_y), (0, 0))
        press(self.app, *["RIGHT"] * 20, *["DOWN"] * 20)
        self.assertEqual((self.app.cursor_x, self.app.cursor_y), (9, 4))
        self.assertEqual(self.send.events, [])  # Moving alone sends nothing.

    def test_pen_mode_draws_while_moving(self):
        press(self.app, "p", "RIGHT", "RIGHT", "p", "RIGHT")
        self.assertEqual(self.app.canvas.row_text(0), "###       ")
        self.assertEqual(len(self.send.events), 3)

    def test_brush_selection(self):
        press(self.app, "b", "*", " ")
        self.assertEqual(self.app.canvas.cell(0, 0)[0], "*")
        self.assertFalse(self.app.awaiting_brush)

    def test_brush_selection_rejects_space_and_can_be_cancelled(self):
        press(self.app, "b", " ")
        self.assertTrue(self.app.awaiting_brush)
        self.assertIn("visible", self.app.message)
        press(self.app, "ESC", " ")
        self.assertEqual(self.app.brush, "#")
        self.assertEqual(self.app.canvas.cell(0, 0)[0], "#")

    def test_brush_selection_takes_keys_literally(self):
        press(self.app, "b", "q")  # 'q' becomes the brush, it does not quit.
        self.assertTrue(self.app.running)
        self.assertEqual(self.app.brush, "q")

    def test_color_keys(self):
        press(self.app, "5", " ")
        self.assertEqual(self.app.canvas.cell(0, 0), ("#", 5))
        press(self.app, "8")  # Not a colour key.
        self.assertEqual(self.app.color, 5)

    def test_erase_sends_blank(self):
        press(self.app, "3", " ", "e")
        self.assertEqual(self.app.canvas.cell(0, 0), (BLANK, 0))
        self.assertEqual(self.send.events[-1]["char"], BLANK)

    def test_clear_all(self):
        press(self.app, " ", "RIGHT", " ", "X")
        self.assertEqual(self.app.canvas.row_text(0), BLANK * 10)
        self.assertEqual(self.send.events[-1], {"type": protocol.CLEAR})

    def test_quit_keys(self):
        for key in ("q", "Q", "CTRL_C"):
            application, _, _ = make_app()
            press(application, key)
            self.assertFalse(application.running, key)

    def test_unknown_key_shows_help_and_changes_nothing(self):
        press(self.app, "z")
        self.assertIn("Unknown key", self.app.message)
        self.assertEqual(self.send.events, [])

    def test_send_failure_is_reported_and_app_keeps_running(self):
        self.send.fail_with = OSError("network unreachable")
        press(self.app, " ")
        self.assertTrue(self.app.running)
        self.assertEqual(self.app.send_errors, 1)
        self.assertIn("Send failed", self.app.message)
        self.assertEqual(self.app.canvas.cell(0, 0)[0], "#")  # Still drawn locally.
        self.send.fail_with = None
        press(self.app, " ")
        self.assertEqual(self.app.sent_count, 1)


class RemoteEventTests(unittest.TestCase):
    def setUp(self):
        self.app, self.send, self.inbox = make_app()

    def remote(self, **fields):
        event = {"type": protocol.DRAW, "seq": 0, "x": 1, "y": 1, "char": "R",
                 "color": 2, "addr": ("192.0.2.9", 40000)}
        event.update(fields)
        self.inbox.put(event)

    def test_queued_events_are_applied(self):
        self.remote(x=1)
        self.remote(x=2, char="S")
        self.assertEqual(self.app.process_remote_events(), 2)
        self.assertEqual(self.app.canvas.row_text(1)[:3], " RS")
        self.assertEqual(self.app.received_count, 2)
        self.assertEqual(self.send.events, [])  # Remote events are not re-sent.

    def test_remote_clear(self):
        press(self.app, " ")
        self.inbox.put({"type": protocol.CLEAR, "seq": 3, "addr": ("192.0.2.9", 1)})
        self.app.process_remote_events()
        self.assertEqual(self.app.canvas.cell(0, 0)[0], BLANK)

    def test_malformed_remote_event_is_counted_not_fatal(self):
        self.inbox.put({"type": "BOGUS"})
        self.inbox.put("not a dict")
        self.remote()
        self.assertEqual(self.app.process_remote_events(), 3)
        self.assertEqual(self.app.rejected_count, 2)
        self.assertEqual(self.app.received_count, 1)

    def test_out_of_bounds_remote_draw_is_ignored(self):
        self.remote(x=500)
        self.app.process_remote_events()
        self.assertEqual(self.app.received_count, 1)
        self.assertEqual([self.app.canvas.row_text(y) for y in range(5)], [BLANK * 10] * 5)

    def test_limit_per_frame(self):
        for i in range(7):
            self.remote(x=i)
        self.assertEqual(self.app.process_remote_events(limit=5), 5)
        self.assertEqual(self.inbox.qsize(), 2)
        self.assertEqual(self.app.process_remote_events(limit=5), 2)

    def test_dead_receiver_is_reported_once(self):
        application, _, _ = make_app(network_ok=lambda: False)
        application.process_remote_events()
        self.assertIn("receiver stopped", application.message)
        application.message = ""
        application.process_remote_events()
        self.assertEqual(application.message, "")


class FakeView:
    """Scripted keys; None means 'no key pressed within the timeout'."""

    def __init__(self, keys, on_idle=None):
        self.keys = list(keys)
        self.renders = 0
        self.on_idle = on_idle

    def render(self, application):
        self.renders += 1

    def read_key(self):
        if not self.keys:
            return "q"  # Safety net so a broken loop cannot hang the test.
        key = self.keys.pop(0)
        if key is None and self.on_idle:
            self.on_idle()
        return key


class RunLoopTests(unittest.TestCase):
    def test_loop_exits_on_quit(self):
        application, send, _ = make_app()
        view = FakeView([" ", "RIGHT", " ", "q", " "])
        run_loop(application, view)
        self.assertFalse(application.running)
        self.assertEqual(len(send.events), 2)
        self.assertEqual(view.keys, [" "])  # Nothing processed after quit.

    def test_remote_events_drawn_while_user_is_idle(self):
        application, _, inbox = make_app()
        arrived = []

        def deliver():
            if not arrived:
                inbox.put({"type": protocol.DRAW, "x": 4, "y": 0, "char": "Z", "color": 0})
                arrived.append(True)

        view = FakeView([None, None, None, "q"], on_idle=deliver)
        run_loop(application, view)
        self.assertEqual(application.canvas.cell(4, 0)[0], "Z")
        self.assertEqual(application.received_count, 1)
        self.assertGreaterEqual(view.renders, 2)

    def test_render_only_when_something_changed(self):
        application, _, _ = make_app()
        view = FakeView([None] * 5 + ["q"])
        run_loop(application, view)
        self.assertEqual(view.renders, 1)


# ---------------------------------------------------------------------------
# CursesView with a fake curses module: no real terminal needed.
# ---------------------------------------------------------------------------


class FakeCursesError(Exception):
    pass


def fake_curses(has_colors=False):
    c = mock.MagicMock(name="curses")
    c.error = FakeCursesError
    c.KEY_UP, c.KEY_DOWN, c.KEY_LEFT, c.KEY_RIGHT, c.KEY_RESIZE = 259, 258, 260, 261, 410
    c.has_colors.return_value = has_colors
    c.color_pair.side_effect = lambda n: n << 8
    return c


class CursesViewTests(unittest.TestCase):
    def make_view(self, rows=24, cols=80, has_colors=False):
        self.curses = fake_curses(has_colors)
        self.scr = mock.MagicMock(name="stdscr")
        self.scr.getmaxyx.return_value = (rows, cols)
        return CursesView(self.scr, self.curses)

    def written(self):
        return [c.args for c in self.scr.addnstr.call_args_list]

    def test_setup_enables_keypad_and_input_timeout(self):
        self.make_view()
        self.scr.keypad.assert_called_once_with(True)
        self.scr.timeout.assert_called_once_with(app.INPUT_TIMEOUT_MS)

    def test_key_translation(self):
        view = self.make_view()
        codes = [-1, 259, 258, 260, 261, 27, 3, ord(" "), ord("#"), ord("q"), 410, 999]
        self.scr.getch.side_effect = codes
        keys = [view.read_key() for _ in codes]
        self.assertEqual(keys, [None, "UP", "DOWN", "LEFT", "RIGHT", "ESC", "CTRL_C",
                                " ", "#", "q", "RESIZE", None])

    def test_render_draws_border_canvas_and_status(self):
        view = self.make_view()
        application, _, _ = make_app(width=60, height=18)
        press(application, "b", "@", " ")
        view.render(application)
        rows = {args[0]: args for args in self.written() if args[1] == 1}
        self.assertTrue(rows[2][2].startswith("@"))  # First canvas row.
        texts = [args[2] for args in self.written()]
        self.assertIn("+" + "-" * 60 + "+", texts)
        self.assertTrue(any(t.startswith("Brush '@'") for t in texts))
        self.scr.move.assert_called_with(2, 1)
        self.scr.refresh.assert_called()

    def test_render_never_writes_last_column(self):
        view = self.make_view(rows=23, cols=62)
        application, _, _ = make_app(width=60, height=18)
        view.render(application)
        for y, x, text, width, attr in self.written():
            self.assertLessEqual(x + width, 61)

    def test_render_uses_colour_pairs(self):
        view = self.make_view(has_colors=True)
        application, _, _ = make_app(width=60, height=18)
        press(application, "2", " ")
        view.render(application)
        attrs = [args[4] for args in self.written() if args[0] == 2 and args[1] == 1]
        self.assertEqual(attrs[0], 2 << 8)

    def test_status_shows_missing_and_duplicate_counts(self):
        view = self.make_view(cols=120)
        application, _, _ = make_app(
            width=60, height=18,
            network_stats=lambda: {"missing": 3, "duplicates": 2})
        view.render(application)
        texts = [args[2] for args in self.written()]
        self.assertTrue(any("Miss 3  Dup 2" in t for t in texts))

    def test_status_omits_counts_without_stats(self):
        view = self.make_view(cols=120)
        application, _, _ = make_app(width=60, height=18)
        view.render(application)
        self.assertFalse(any("Miss" in args[2] for args in self.written()))

    def test_small_terminal_shows_message_instead_of_canvas(self):
        view = self.make_view(rows=10, cols=40)
        application, _, _ = make_app(width=60, height=18)
        view.render(application)
        texts = [args[2] for args in self.written()]
        self.assertTrue(texts[0].startswith("Terminal too small: need 62x23, have 40x10"))
        self.assertEqual(len(texts), 2)


# ---------------------------------------------------------------------------
# main(): startup failures, Ctrl+C and cleanup (network and curses mocked).
# ---------------------------------------------------------------------------


class MainTests(unittest.TestCase):
    def setUp(self):
        self.start = mock.patch.object(network, "start_network").start()
        self.start.return_value = mock.Mock(
            running=True, local_address=("192.0.2.1", 1),
            tracker=mock.Mock(totals=lambda: {"missing": 0, "duplicates": 0}))
        self.stop = mock.patch.object(network, "stop_network").start()
        mock.patch.object(app.logging, "basicConfig").start()  # No log file in tests.
        self.addCleanup(mock.patch.stopall)
        self.curses = fake_curses()
        mock.patch.object(app, "load_curses", return_value=self.curses).start()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = app.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_missing_curses_exits_before_networking(self):
        app.load_curses.return_value = None
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("requirements.txt", err)
        self.start.assert_not_called()

    def test_network_start_failure_is_explained(self):
        self.start.side_effect = OSError("cannot assign requested address")
        code, _, err = self.run_main("--interface", "10.9.9.9")
        self.assertEqual(code, 1)
        self.assertIn("10.9.9.9", err)
        self.assertIn("ipconfig", err)
        self.curses.wrapper.assert_not_called()

    def test_app_shows_tracker_counts(self):
        rendered = []

        def fake_wrapper(func):
            scr = mock.MagicMock()
            scr.getmaxyx.return_value = (24, 120)
            scr.getch.side_effect = [ord("q")]
            scr.addnstr.side_effect = lambda y, x, text, n, attr: rendered.append(text)
            func(scr)

        self.curses.wrapper.side_effect = fake_wrapper
        self.run_main()
        self.assertTrue(any("Miss 0  Dup 0" in t for t in rendered))

    def test_interface_argument_is_passed_to_network(self):
        self.run_main("--interface", "192.0.2.77")
        self.assertEqual(self.start.call_args.kwargs["interface"], "192.0.2.77")

    def test_normal_exit_stops_network(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.curses.wrapper.assert_called_once()
        self.stop.assert_called_once()
        self.assertIn("Stopped", out)

    def test_ctrl_c_exits_cleanly_and_stops_network(self):
        self.curses.wrapper.side_effect = KeyboardInterrupt
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.stop.assert_called_once()

    def test_unexpected_error_still_stops_network(self):
        self.curses.wrapper.side_effect = RuntimeError("UI crashed")
        with self.assertRaises(RuntimeError):
            self.run_main()
        self.stop.assert_called_once()

    def test_wrapper_runs_loop_with_curses_view(self):
        def fake_wrapper(func):
            scr = mock.MagicMock()
            scr.getmaxyx.return_value = (24, 80)
            scr.getch.side_effect = [ord(" "), ord("q")]
            func(scr)

        self.curses.wrapper.side_effect = fake_wrapper
        with mock.patch.object(network, "send_event") as send:
            code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        send.assert_called_once()
        self.assertIn("Sent 1", out)


# ---------------------------------------------------------------------------
# Real multicast on this host: two app instances exchange drawing.
# ---------------------------------------------------------------------------


def multicast_available(port):
    try:
        recv_sock = network.create_receive_socket(port=port)
    except OSError:
        return False
    try:
        with network.create_send_socket(port=port) as send_sock:
            recv_sock.settimeout(1.0)
            send_sock.send(b"probe")
            return recv_sock.recvfrom(64)[0] == b"probe"
    except OSError:
        return False
    finally:
        recv_sock.close()


TEST_PORT = random.randint(40000, 49999)


@unittest.skipUnless(
    multicast_available(TEST_PORT), "IPv4 multicast loopback unavailable on this host"
)
class TwoAppLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.apps = []
        for _ in range(2):
            inbox = queue.Queue()
            peer = network.MulticastPeer(inbox.put, port=TEST_PORT)
            peer.start()
            self.addCleanup(peer.stop)
            self.apps.append(CanvasApp(Canvas(10, 5), peer.send, inbox))

    def wait_for(self, application, count):
        # The receive threads fill the queues; poll the way the UI loop does.
        for _ in range(100):
            application.process_remote_events()
            if application.received_count >= count:
                return
            time.sleep(0.02)
        self.fail(f"expected {count} remote events, got {application.received_count}")

    def test_drawing_appears_on_the_other_canvas_exactly_once(self):
        a, b = self.apps
        press(a, "4", " ", "RIGHT", " ")
        self.wait_for(b, 2)
        self.assertEqual(b.canvas.row_text(0)[:2], "##")
        self.assertEqual(b.canvas.cell(0, 0), ("#", 4))
        # A never receives its own frames back.
        a.process_remote_events()
        self.assertEqual(a.received_count, 0)

    def test_both_directions_and_clear(self):
        a, b = self.apps
        press(a, " ")
        press(b, "DOWN", "b", "o", " ")
        self.wait_for(b, 1)
        self.wait_for(a, 1)
        self.assertEqual(a.canvas.cell(0, 1)[0], "o")
        self.assertEqual(b.canvas.cell(0, 0)[0], "#")
        press(a, "X")
        self.wait_for(b, 2)
        self.assertEqual([b.canvas.row_text(y) for y in range(5)], [BLANK * 10] * 5)


if __name__ == "__main__":
    unittest.main()
