import itertools
import os
import queue
import random
import tempfile
import threading
import unittest

import lamport
import persistence
import protocol
from app import CanvasApp
from canvas import BLANK, DEFAULT_COLOR, NO_STAMP, Canvas, CanvasError
from lamport import LamportClock
from session import Session
from tests.simnet import SimNetwork

A, B, C = 0xA, 0xB, 0xC  # Peer IDs; B wins ties against A.


def draw(lamport, peer, x=1, y=1, char="#", color=1):
    return {"type": "DRAW", "x": x, "y": y, "char": char, "color": color,
            "lamport": lamport, "peer": peer}


def clear(lamport, peer):
    return {"type": "CLEAR", "lamport": lamport, "peer": peer}


def visible(canvas):
    return [(canvas.row_text(y), canvas.row_colors(y)) for y in range(canvas.height)]


class ClockTests(unittest.TestCase):
    def test_tick_increments_from_zero(self):
        clock = LamportClock()
        self.assertEqual(clock.time, 0)
        self.assertEqual([clock.tick() for _ in range(3)], [1, 2, 3])

    def test_observe_moves_past_received_time(self):
        clock = LamportClock()
        self.assertEqual(clock.observe(10), 11)
        self.assertEqual(clock.tick(), 12)

    def test_observe_of_older_time_still_advances(self):
        clock = LamportClock(start=20)
        self.assertEqual(clock.observe(5), 21)

    def test_event_after_seeing_another_is_later(self):
        a, b = LamportClock(), LamportClock()
        for _ in range(50):
            a.tick()            # A is busy.
        seen = a.tick()
        b.observe(seen)         # B sees A's latest edit...
        self.assertGreater(b.tick(), seen)  # ...so B's next edit is later.

    def test_clock_saturates_at_header_maximum(self):
        clock = LamportClock(start=lamport.MAX_TIME - 1)
        self.assertEqual(clock.tick(), lamport.MAX_TIME)
        self.assertEqual(clock.tick(), lamport.MAX_TIME)
        self.assertEqual(clock.observe(lamport.MAX_TIME), lamport.MAX_TIME)

    def test_thread_safe_ticks_are_unique(self):
        clock = LamportClock()
        results = []
        lock = threading.Lock()

        def worker():
            local = [clock.tick() for _ in range(1000)]
            with lock:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), list(range(1, 4001)))


class LastWriterWinsTests(unittest.TestCase):
    def setUp(self):
        self.canvas = Canvas(10, 5)

    def test_newer_stamp_wins_and_older_is_ignored(self):
        self.assertTrue(self.canvas.apply_event(draw(5, A, char="n")))
        self.assertFalse(self.canvas.apply_event(draw(4, B, char="o")))
        self.assertEqual(self.canvas.cell(1, 1)[0], "n")
        self.assertTrue(self.canvas.apply_event(draw(6, A, char="m")))
        self.assertEqual(self.canvas.cell(1, 1)[0], "m")
        self.assertEqual(self.canvas.cell_stamp(1, 1), (6, A))

    def test_peer_id_breaks_ties(self):
        self.canvas.apply_event(draw(3, B, char="b"))
        self.assertFalse(self.canvas.apply_event(draw(3, A, char="a")))
        self.assertEqual(self.canvas.cell(1, 1)[0], "b")
        other = Canvas(10, 5)
        other.apply_event(draw(3, A, char="a"))
        other.apply_event(draw(3, B, char="b"))
        self.assertEqual(other.cell(1, 1)[0], "b")

    def test_same_event_twice_is_idempotent(self):
        self.assertTrue(self.canvas.apply_event(draw(3, A)))
        self.assertFalse(self.canvas.apply_event(draw(3, A)))

    def test_cells_are_independent(self):
        self.canvas.apply_event(draw(9, A, x=0))
        self.assertTrue(self.canvas.apply_event(draw(1, B, x=2)))

    def test_delayed_draw_after_clear_stays_erased(self):
        self.canvas.apply_event(clear(10, A))
        self.assertFalse(self.canvas.apply_event(draw(9, B)))
        self.assertEqual(self.canvas.cell(1, 1), (BLANK, DEFAULT_COLOR))
        self.assertTrue(self.canvas.apply_event(draw(11, B)))

    def test_clear_erases_older_cells_only(self):
        self.canvas.apply_event(draw(4, A, x=0, char="o"))
        self.canvas.apply_event(draw(8, A, x=1, char="n"))
        self.assertTrue(self.canvas.apply_event(clear(6, B)))
        self.assertEqual(self.canvas.cell(0, 1), (BLANK, DEFAULT_COLOR))
        self.assertEqual(self.canvas.cell(1, 1)[0], "n")  # Drawn after the CLEAR.
        self.assertEqual(self.canvas.clear_stamp, (6, B))
        self.assertEqual(self.canvas.cell_stamp(0, 1), NO_STAMP)

    def test_delayed_clear_is_ignored_after_newer_clear(self):
        self.canvas.apply_event(clear(10, A))
        self.canvas.apply_event(draw(11, A))
        self.assertFalse(self.canvas.apply_event(clear(7, B)))
        self.assertEqual(self.canvas.clear_stamp, (10, A))
        self.assertEqual(self.canvas.cell(1, 1)[0], "#")

    def test_stamped_events_beat_unstamped_cells(self):
        self.canvas.draw(1, 1, "u", 3)  # v1-style write: NO_STAMP.
        self.assertTrue(self.canvas.apply_event(draw(0, A, char="s")))
        self.assertEqual(self.canvas.cell(1, 1)[0], "s")

    def test_out_of_bounds_stamped_draw_is_ignored(self):
        self.assertFalse(self.canvas.apply_event(draw(5, A, x=10)))

    def test_malformed_stamps_raise(self):
        for event in (draw(-1, A), draw(2**32, A), draw("3", A), draw(3, None),
                      draw(3, True), clear(1.5, A), draw(3, A, char="\x00")):
            with self.subTest(event=event), self.assertRaises(CanvasError):
                self.canvas.apply_event(event)
        self.assertEqual(visible(self.canvas), visible(Canvas(10, 5)))


class ArrivalOrderTests(unittest.TestCase):
    """The same events in any order give the same canvas."""

    EVENTS = [
        draw(1, A, x=0, char="a"), draw(1, B, x=0, char="b"),   # Concurrent, same cell.
        draw(2, A, x=1, char="c"), draw(5, C, x=1, char="d"),
        clear(4, B),
        draw(3, C, x=2, char="e"),                              # Older than the CLEAR.
        draw(6, A, x=2, char="f"), draw(6, B, x=3, char="g"),
        clear(4, A),                                             # Loses the tie to B's CLEAR.
        draw(7, C, x=0, char="h"), draw(7, A, x=0, char="i"),
    ]

    @staticmethod
    def final(events, width=5, height=3):
        canvas = Canvas(width, height)
        for event in events:
            canvas.apply_event({**event, "y": 1} if event["type"] == "DRAW" else event)
        return visible(canvas), canvas.clear_stamp

    def test_every_order_of_a_small_set_converges(self):
        events = self.EVENTS[:6]
        results = {repr(self.final(order)) for order in itertools.permutations(events)}
        self.assertEqual(len(results), 1)

    def test_random_orders_and_duplicates_converge(self):
        rng = random.Random(1234)
        expected = self.final(self.EVENTS)
        for _ in range(500):
            order = self.EVENTS + rng.sample(self.EVENTS, 4)  # Some duplicated.
            rng.shuffle(order)
            self.assertEqual(self.final(order), expected)

    def test_expected_final_state(self):
        canvas = Canvas(5, 3)
        for event in self.EVENTS:
            canvas.apply_event({**event, "y": 1} if event["type"] == "DRAW" else event)
        self.assertEqual(canvas.cell(0, 1)[0], "h")   # (7, C) beats (7, A).
        self.assertEqual(canvas.cell(1, 1)[0], "d")   # (5, C) is after the CLEAR (4, B).
        self.assertEqual(canvas.cell(2, 1)[0], "f")   # (3, C) was erased; (6, A) is later.
        self.assertEqual(canvas.cell(3, 1)[0], "g")
        self.assertEqual(canvas.clear_stamp, (4, B))  # (4, A) lost the tie.


class V1BehaviourTests(unittest.TestCase):
    def test_unstamped_events_apply_in_arrival_order(self):
        canvas = Canvas(10, 5)
        canvas.apply_event({"type": "DRAW", "x": 1, "y": 1, "char": "a", "color": 0, "seq": 9})
        canvas.apply_event({"type": "DRAW", "x": 1, "y": 1, "char": "b", "color": 0, "seq": 1})
        self.assertEqual(canvas.cell(1, 1)[0], "b")
        self.assertTrue(canvas.apply_event({"type": "CLEAR", "seq": 0}))
        self.assertEqual(visible(canvas), visible(Canvas(10, 5)))

    def test_decoded_v1_event_has_no_stamp(self):
        event = protocol.decode_event(protocol.encode_event(
            {"type": "DRAW", "seq": 3, "x": 1, "y": 1, "char": "#", "color": 2}))
        canvas = Canvas(10, 5)
        self.assertTrue(canvas.apply_event(event))
        self.assertEqual(canvas.cell_stamp(1, 1), NO_STAMP)

    def test_unstamped_clear_resets_stamps(self):
        canvas = Canvas(10, 5)
        canvas.apply_event(clear(9, A))
        canvas.clear()
        self.assertEqual(canvas.clear_stamp, NO_STAMP)
        self.assertTrue(canvas.apply_event(draw(1, A)))


class PersistenceWithStampsTests(unittest.TestCase):
    def test_save_format_is_unchanged_and_load_resets_stamps(self):
        canvas = Canvas(10, 5)
        canvas.apply_event(draw(5, A, char="s"))
        canvas.apply_event(clear(3, B))
        self.assertEqual(canvas.snapshot()["cells"],
                         [{"x": 1, "y": 1, "char": "s", "color": 1}])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "c.json")
            persistence.save_canvas(canvas, path)
            loaded = Canvas(10, 5)
            loaded.apply_event(clear(50, A))
            persistence.load_canvas(loaded, path)
        self.assertEqual(visible(loaded), visible(canvas))
        self.assertEqual(loaded.cell_stamp(1, 1), NO_STAMP)
        self.assertEqual(loaded.clear_stamp, NO_STAMP)


# ---------------------------------------------------------------------------
# Session and CanvasApp in v2 mode
# ---------------------------------------------------------------------------


class FakePeer:
    def __init__(self, peer_id):
        self.peer_id = peer_id
        self.sent = []

    def send(self, event):
        self.sent.append(dict(event))
        return len(self.sent) - 1


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.peer = FakePeer(A)
        canvas = Canvas(10, 5)
        self.app = CanvasApp(canvas, self.peer.send, queue.Queue(),
                             session=Session(canvas, self.peer))

    def test_local_draw_is_stamped_before_apply_and_send(self):
        self.app.handle_key(" ")
        self.app.handle_key(" ")
        sent = self.peer.sent
        self.assertEqual([(e["lamport"], e["peer"]) for e in sent], [(1, A), (2, A)])
        self.assertEqual(self.app.canvas.cell_stamp(0, 0), (2, A))

    def test_local_clear_is_stamped(self):
        self.app.handle_key("X")
        self.assertEqual(self.peer.sent[-1], {"type": "CLEAR", "lamport": 1, "peer": A})
        self.assertEqual(self.app.canvas.clear_stamp, (1, A))

    def test_received_events_advance_the_clock(self):
        self.app.inbox.put(draw(40, B, x=5))
        self.app.process_remote_events()
        self.assertEqual(self.app.session.clock.time, 41)
        self.app.handle_key(" ")
        self.assertEqual(self.peer.sent[-1]["lamport"], 42)

    def test_stale_remote_event_still_advances_clock_but_does_not_apply(self):
        self.app.handle_key(" ")              # (1, A) at (0, 0).
        self.app.inbox.put(draw(1, 0x1, x=0, y=0, char="z"))  # Smaller peer ID: loses.
        self.app.process_remote_events()
        self.assertEqual(self.app.canvas.cell(0, 0)[0], "#")
        self.assertEqual(self.app.session.clock.time, 2)
        self.assertEqual(self.app.received_count, 1)

    def test_local_edit_after_remote_edit_wins_everywhere(self):
        self.app.inbox.put(draw(30, 0xFFFFFFFF, x=0, y=0, char="r"))
        self.app.process_remote_events()
        self.app.handle_key(" ")
        self.assertEqual(self.app.canvas.cell(0, 0)[0], "#")
        sent = self.peer.sent[-1]
        self.assertGreater((sent["lamport"], sent["peer"]), (30, 0xFFFFFFFF))

    def test_malformed_remote_event_is_rejected_not_fatal(self):
        self.app.inbox.put(draw("x", B))
        self.app.process_remote_events()
        self.assertEqual(self.app.rejected_count, 1)
        self.assertEqual(self.app.session.clock.time, 0)


class TwoAppConvergenceTests(unittest.TestCase):
    """CanvasApps with Sessions over the in-memory v2 network."""

    def make_app(self, net, peer_id):
        peer, recorder = net.new_peer(peer_id=peer_id)
        canvas = Canvas(10, 5)
        return CanvasApp(canvas, peer.send, recorder.events, session=Session(canvas, peer))

    def setUp(self):
        self.net = SimNetwork(seed=7)
        self.a = self.make_app(self.net, A)
        self.b = self.make_app(self.net, B)

    def settle(self, **delivery):
        self.net.deliver(**delivery)
        for application in (self.a, self.b):
            application.process_remote_events()

    def test_concurrent_edits_of_one_cell_converge(self):
        # The divergence case from the roadmap (section 9.1): both draw at once.
        self.a.brush, self.b.brush = "x", "o"
        self.a.handle_key(" ")
        self.b.handle_key(" ")
        self.settle()
        self.assertEqual(self.a.canvas.cell(0, 0)[0], "o")  # (1, B) beats (1, A).
        self.assertEqual(visible(self.a.canvas), visible(self.b.canvas))

    def test_reordered_and_duplicated_delivery_converges(self):
        for _ in range(3):
            self.a.handle_key(" ")
            self.a.handle_key("RIGHT")
            self.b.handle_key(" ")
            self.b.handle_key("DOWN")
        self.settle()
        self.b.handle_key("X")
        for _ in range(4):
            self.a.handle_key("RIGHT")
            self.a.handle_key(" ")
        self.settle(shuffle=True, duplicate=True)
        self.assertEqual(visible(self.a.canvas), visible(self.b.canvas))
        self.assertEqual(self.a.canvas.clear_stamp, self.b.canvas.clear_stamp)


if __name__ == "__main__":
    unittest.main()
