import io
import random
import unittest
from contextlib import redirect_stderr
from unittest import mock

import app
import network
import protocol_v2
import recovery
import session as session_module
import sync
from app import CanvasApp
from canvas import BLANK, Canvas
from protocol import ProtocolError
from recovery import Recovery, ranges
from session import Session
from tracker import SequenceTracker
from tests.simnet import SimNetwork
from tests.test_sync import FakeClock, FixedDelays, visible

A, B, C = 0xA, 0xB, 0xC


def info(data):
    return protocol_v2.decode_message(data)


def kind(data):
    return protocol_v2.MessageType(data[3]).name


class RecoveryProtocolTests(unittest.TestCase):
    def round_trip(self, message):
        data = protocol_v2.encode_message({**message, "peer": A, "seq": 9})
        return data, protocol_v2.decode_message(data)

    def test_heartbeat_nack_and_not_available(self):
        data, decoded = self.round_trip({"type": "HEARTBEAT"})
        self.assertEqual((len(data), decoded["seq"]), (20, 9))
        data, decoded = self.round_trip({"type": "NACK", "target": B, "first": 2**32 - 1,
                                         "count": 3})
        self.assertEqual(len(data), 30)
        self.assertEqual((decoded["target"], decoded["first"], decoded["count"]),
                         (B, 2**32 - 1, 3))
        data, decoded = self.round_trip({"type": "NOT_AVAILABLE", "first": 4, "count": 2,
                                         "oldest": 40})
        self.assertEqual(len(data), 30)
        self.assertEqual((decoded["first"], decoded["count"], decoded["oldest"]), (4, 2, 40))

    def test_invalid_recovery_messages(self):
        for message in ({"type": "NACK", "target": 0, "first": 0, "count": 1},
                        {"type": "NACK", "target": B, "first": 0, "count": 0},
                        {"type": "NACK", "target": B, "first": -1, "count": 1},
                        {"type": "NOT_AVAILABLE", "first": 0, "count": 0, "oldest": 0},
                        {"type": "NACK", "target": B}):
            with self.subTest(message=message), self.assertRaises(ProtocolError):
                protocol_v2.encode_message({**message, "peer": A, "seq": 0})
        good = protocol_v2.encode_message({"type": "NACK", "peer": A, "seq": 0,
                                           "target": B, "first": 1, "count": 1})
        zero_count = good[:-2] + b"\x00\x00"
        zero_target = good[:20] + b"\x00\x00\x00\x00" + good[24:]
        for data, pattern in ((zero_count, "at least 1"), (zero_target, "target peer ID 0")):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ProtocolError, pattern):
                protocol_v2.decode_message(data)


class TrackerHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SequenceTracker()

    def test_first_heartbeat_sets_baseline_without_missing(self):
        self.assertEqual(self.tracker.observe_heartbeat(A, 50), 0)
        self.assertEqual(self.tracker.missing(A), [])
        self.assertEqual(self.tracker.stats(A).received, 0)
        self.tracker.observe(A, 50)
        self.assertEqual(self.tracker.stats(A).gaps, 0)

    def test_heartbeat_reveals_tail_loss(self):
        for seq in (0, 1, 2):
            self.tracker.observe(A, seq)
        self.assertEqual(self.tracker.observe_heartbeat(A, 5), 2)  # 3 and 4 never came.
        self.assertEqual(self.tracker.missing(A), [3, 4])
        self.assertEqual(self.tracker.observe(A, 4).name, "LATE")
        self.assertEqual(self.tracker.stats(A).missing, 1)

    def test_heartbeat_with_nothing_new_changes_nothing(self):
        self.tracker.observe(A, 7)
        for next_seq in (8, 3, 0):
            self.assertEqual(self.tracker.observe_heartbeat(A, next_seq), 0)
        self.assertEqual(self.tracker.missing(A), [])

    def test_heartbeat_across_wraparound(self):
        self.tracker.observe(A, 2**32 - 2)
        self.assertEqual(self.tracker.observe_heartbeat(A, 1), 2)
        self.assertEqual(self.tracker.missing(A), [2**32 - 1, 0])

    def test_forget_missing(self):
        self.tracker.observe(A, 0)
        self.tracker.observe(A, 4)
        self.tracker.forget_missing(A, [1, 2])
        self.assertEqual(self.tracker.missing(A), [3])
        self.assertEqual(self.tracker.observe(A, 1).name, "DUPLICATE")
        self.assertEqual(self.tracker.senders(), [A])


class RangesTests(unittest.TestCase):
    def test_contiguous_runs(self):
        self.assertEqual(ranges([5, 3, 4, 9, 10, 20]), [(3, 3), (9, 2), (20, 1)])

    def test_run_across_wraparound(self):
        self.assertEqual(ranges([2**32 - 2, 2**32 - 1, 0, 1]), [(2**32 - 2, 4)])

    def test_runs_are_capped(self):
        runs = ranges(range(600))
        self.assertEqual(runs, [(0, 256), (256, 256), (512, 88)])


class SenderTests(unittest.TestCase):
    """The original sender's side: history, retransmission, NOT_AVAILABLE."""

    def setUp(self):
        self.net = SimNetwork()
        self.clock = FakeClock()
        self.sender, _ = self.net.new_peer(peer_id=A, history_size=4, clock=self.clock)
        self.other, self.events = self.net.new_peer(peer_id=B)
        for x in range(6):
            self.sender.send({"type": "DRAW", "x": x, "y": 0, "char": "#", "color": 1})
        self.net.deliver()
        self.net.sent.clear()

    def nack(self, first, count, target=A):
        self.net.inject(protocol_v2.encode_message(
            {"type": "NACK", "peer": B, "seq": 0, "target": target, "first": first,
             "count": count}), source=self.other.local_address)
        self.net.deliver()
        return [info(d) for d, src in self.net.sent if src == self.sender.local_address]

    def test_history_is_bounded(self):
        self.assertEqual(list(self.sender._history), [2, 3, 4, 5])

    def test_nack_is_answered_with_flagged_original(self):
        answers = self.nack(3, 2)
        self.assertEqual([(m["type"], m["seq"], m["x"], m["retransmit"]) for m in answers],
                         [("DRAW", 3, 3, True), ("DRAW", 4, 4, True)])
        self.assertEqual(self.sender.retransmitted, 2)

    def test_retransmission_is_rate_limited(self):
        self.nack(3, 1)
        self.clock.now += 0.1
        self.assertEqual(len(self.nack(3, 1)), 1)          # Still only the first answer.
        self.clock.now += 0.1
        self.assertEqual(len(self.nack(3, 1)), 2)          # 0.2 s later: answered again.

    def test_evicted_events_get_not_available(self):
        answers = self.nack(0, 3)  # 0 and 1 were evicted; 2 is still there.
        kinds = [(m["type"], m.get("seq")) for m in answers]
        self.assertIn(("DRAW", 2), kinds)
        report = [m for m in answers if m["type"] == "NOT_AVAILABLE"][0]
        self.assertEqual((report["first"], report["count"], report["oldest"]), (0, 2, 2))
        self.assertEqual(len(self.nack(0, 3)), 2)  # Rate-limited: nothing new.

    def test_nack_for_another_peer_is_ignored(self):
        self.assertEqual(self.nack(3, 2, target=C), [])

    def test_huge_nack_is_capped(self):
        sender, _ = self.net.new_peer(peer_id=0xD, history_size=1000, clock=self.clock)
        for x in range(600):
            sender.send({"type": "DRAW", "x": x % 50, "y": 0, "char": "#", "color": 1})
        self.net.deliver()
        self.net.sent.clear()
        self.nack(0, 600, target=0xD)
        self.assertEqual(sender.retransmitted, network.MAX_NACK_COUNT)

    def test_receiver_drops_retransmission_it_already_has(self):
        before = len(self.events.drain())
        self.assertEqual(before, 6)
        self.nack(3, 1)
        self.assertEqual(self.events.drain(), [])
        self.assertGreater(self.other.tracker.stats(A).duplicates, 0)


class RecoveryUnitTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SequenceTracker()
        self.sent = []
        self.lost = []
        self.rec = Recovery(self.tracker, self.sent.append, FixedDelays(0.1),
                            lambda peer, seqs: self.lost.append((peer, seqs)))
        for seq in (0, 4):
            self.tracker.observe(A, seq)  # 1, 2, 3 missing.

    def test_first_nack_waits_for_the_random_delay(self):
        self.rec.tick(0.0)
        self.assertEqual(self.sent, [])
        self.rec.tick(0.1)
        self.assertEqual(self.sent, [{"type": "NACK", "target": A, "first": 1, "count": 3}])

    def test_retries_back_off_then_give_up(self):
        times = []
        now = 0.0
        while now < 5:
            before = len(self.sent)
            self.rec.tick(now)
            if len(self.sent) > before:
                times.append(round(now, 2))
            now = round(now + 0.01, 2)
        # +0.2 s, then +0.4 s (within one 10 ms step: 0.1 + 0.2 is not exact).
        self.assertEqual(len(times), 3)
        for got, want in zip(times, [0.1, 0.3, 0.7]):
            self.assertAlmostEqual(got, want, delta=0.011)
        self.assertEqual(self.lost, [(A, [1, 2, 3])])       # Given up after 3 NACKs...
        self.assertEqual(self.tracker.missing(A), [])        # ...and never asked again.
        self.assertEqual(self.rec.stats["unrecoverable"], 3)

    def test_arrival_counts_as_recovered(self):
        self.rec.tick(0.1)
        self.tracker.observe(A, 2)
        self.rec.tick(0.2)
        self.assertEqual(self.rec.stats["recovered"], 1)
        self.assertEqual(sorted(self.rec.entries), [(A, 1), (A, 3)])

    def test_nack_from_another_peer_suppresses_ours(self):
        self.rec.tick(0.0)
        self.rec.on_nack({"target": A, "first": 1, "count": 3}, now=0.05)
        self.rec.tick(0.1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rec.stats["suppressed"], 3)
        self.rec.tick(0.25)
        self.assertEqual(len(self.sent), 1)  # Our own retry after the back-off.

    def test_not_available_gives_up_immediately(self):
        self.rec.tick(0.0)
        self.rec.on_not_available({"peer": A, "first": 1, "count": 2, "oldest": 3})
        self.assertEqual(self.lost, [(A, [1, 2])])
        self.assertEqual(sorted(self.rec.entries), [(A, 3)])


class SessionRecoveryTests(unittest.TestCase):
    """Whole apps over the in-memory network with deterministic loss."""

    def setUp(self):
        self.net = SimNetwork(seed=21)
        self.clock = FakeClock()

    def make_app(self, peer_id, **peer_options):
        peer, recorder = self.net.new_peer(peer_id=peer_id, clock=self.clock, **peer_options)
        canvas = Canvas(20, 10)
        session = Session(canvas, peer, time_source=self.clock, rng=random.Random(peer_id))
        application = CanvasApp(canvas, peer.send, recorder.events, session=session)
        application.peer = peer
        return application

    def run_for(self, apps, seconds, step=0.05):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.net.deliver()
            for application in apps:
                application.process_remote_events()
                application.tick()
            self.clock.now = round(self.clock.now + step, 3)
        self.net.deliver()
        for application in apps:
            application.process_remote_events()

    def draw(self, application, x, char="#"):
        application.cursor_x, application.cursor_y = x, 0
        application.brush = char
        application.handle_key(" ")

    def drop_once(self, predicate):
        dropped = []

        def rule(data, source, peer):
            if not dropped and predicate(data, source, peer):
                dropped.append(info(data))
                return True
            return False

        self.net.drop = rule
        return dropped

    def setup_pair(self, **sender_options):
        a = self.make_app(A, **sender_options)
        b = self.make_app(B)
        self.run_for([a, b], 0.2)  # Heartbeats give each a baseline for the other.
        return a, b

    def test_dropped_draw_is_recovered(self):
        a, b = self.setup_pair()
        dropped = self.drop_once(lambda d, s, p: kind(d) == "DRAW" and p is b.peer
                                 and info(d)["x"] == 1)
        for x in range(3):
            self.draw(a, x)
        self.run_for([a, b], 1.0)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(visible(b.canvas), visible(a.canvas))
        self.assertEqual(b.session.recovery.stats["recovered"], 1)
        self.assertEqual(a.peer.retransmitted, 1)
        self.assertEqual(b.received_count, 3)  # Applied once, not twice.

    def test_dropped_clear_is_recovered(self):
        a, b = self.setup_pair()
        self.draw(a, 0)
        self.draw(a, 1)
        self.run_for([a, b], 0.2)
        dropped = self.drop_once(lambda d, s, p: kind(d) == "CLEAR" and p is b.peer)
        a.handle_key("X")
        self.draw(a, 5, char="n")  # Drawn after the CLEAR: must survive it.
        self.run_for([a, b], 1.0)
        self.assertEqual(dropped[0]["type"], "CLEAR")
        self.assertEqual(b.canvas.cell(0, 0)[0], BLANK)
        self.assertEqual(b.canvas.cell(5, 0)[0], "n")
        self.assertEqual(visible(b.canvas), visible(a.canvas))

    def test_lost_last_event_is_found_by_heartbeat(self):
        a, b = self.setup_pair()
        self.drop_once(lambda d, s, p: kind(d) == "DRAW" and p is b.peer)
        self.draw(a, 7)  # The only event: nothing after it reveals the gap.
        self.run_for([a, b], 0.3)
        self.assertEqual(b.canvas.cell(7, 0)[0], BLANK)
        self.run_for([a, b], 1.5)  # The next heartbeat exposes it.
        self.assertEqual(b.canvas.cell(7, 0)[0], "#")

    def test_reordered_and_duplicated_packets_apply_once(self):
        a, b = self.setup_pair()
        for x in range(8):
            self.draw(a, x, char=str(x))
        self.net.deliver(shuffle=True, duplicate=True)
        self.run_for([a, b], 1.0)
        self.assertEqual(visible(b.canvas), visible(a.canvas))
        self.assertEqual(b.received_count, 8)
        self.assertEqual(b.session.recovery.stats["nacks_sent"], 0)

    def test_expired_sender_history_falls_back_to_snapshot(self):
        a, b = self.setup_pair(history_size=2)
        dropped = self.drop_once(lambda d, s, p: kind(d) == "DRAW" and p is b.peer)
        for x in range(5):
            self.draw(a, x)  # Event 0 is lost and soon leaves A's 2-event history.
        self.run_for([a, b], 2.0)
        self.assertEqual(dropped[0]["seq"], 0)
        self.assertGreater(a.peer.not_available_sent, 0)
        self.assertEqual(b.session.recovery.stats["unrecoverable"], 1)
        self.assertEqual(b.session.fallbacks, 1)
        self.assertEqual(b.session.sync.state, sync.SYNCED)
        self.assertEqual(visible(b.canvas), visible(a.canvas))

    def test_nack_suppression_between_receivers(self):
        a, b, c = self.make_app(A), self.make_app(B), self.make_app(C)
        b.session.recovery.rng = FixedDelays(0.05)
        c.session.recovery.rng = FixedDelays(0.14)
        self.run_for([a, b, c], 0.2)
        # B and C both lose event 1; only A has it.
        self.net.drop = lambda d, s, p: (kind(d) == "DRAW" and info(d)["x"] == 1
                                         and not info(d)["retransmit"] and p is not a.peer)
        for x in range(3):
            self.draw(a, x)
        self.run_for([a, b, c], 1.0)
        nack_senders = [info(d)["peer"] for d, _ in self.net.sent if kind(d) == "NACK"]
        self.assertEqual(nack_senders, [B])
        self.assertEqual(c.session.recovery.stats["suppressed"], 1)
        for receiver in (b, c):
            self.assertEqual(visible(receiver.canvas), visible(a.canvas))

    def test_retry_exhaustion_is_bounded_and_falls_back(self):
        a, b = self.setup_pair()
        # Every copy of event 1 to B is lost, including retransmissions.
        self.net.drop = lambda d, s, p: (kind(d) == "DRAW" and info(d)["x"] == 1
                                         and p is b.peer)
        for x in range(3):
            self.draw(a, x)
        self.run_for([a, b], 10.0)
        nacks = [d for d, _ in self.net.sent if kind(d) == "NACK"]
        self.assertEqual(len(nacks), recovery.MAX_NACKS)    # Not retried forever.
        self.assertEqual(a.peer.retransmitted, recovery.MAX_NACKS)
        self.assertEqual(b.session.recovery.stats["unrecoverable"], 1)
        self.assertEqual(b.session.fallbacks, 1)
        self.assertEqual(visible(b.canvas), visible(a.canvas))  # Snapshot repaired it.
        sent_before = len(self.net.sent)
        self.run_for([a, b], 10.0)
        extra = [d for d, _ in self.net.sent[sent_before:] if kind(d) != "HEARTBEAT"]
        self.assertEqual(extra, [])  # Quiet: only heartbeats remain.

    def test_network_unavailable_is_not_fatal(self):
        a, b = self.setup_pair()
        self.net.down = True
        with self.assertLogs("session", level="WARNING"):
            self.draw(a, 0)
            self.run_for([a, b], 3.0)
        self.assertGreater(a.session.send_errors, 0)
        self.assertEqual(a.send_errors, 1)  # The DRAW itself.
        self.assertTrue(a.running and b.running)
        self.net.down = False
        self.draw(a, 1)
        self.run_for([a, b], 1.0)
        self.assertEqual(b.canvas.cell(1, 0)[0], "#")

    def test_random_loss_converges_with_recovery(self):
        apps = [self.make_app(peer_id, loss_rate=0.25, loss_seed=peer_id)
                for peer_id in (A, B, C)]
        self.run_for(apps, 0.3)
        rng = random.Random(99)
        for step in range(60):
            writer = apps[step % 3]
            self.draw(writer, rng.randrange(20), char=rng.choice("abcxyz"))
            if step == 30:
                writer.handle_key("X")
            self.run_for(apps, 0.05)
        self.run_for(apps, 8.0)
        dropped = sum(application.peer.simulated_drops for application in apps)
        self.assertGreater(dropped, 20)
        self.assertEqual(visible(apps[0].canvas), visible(apps[1].canvas))
        self.assertEqual(visible(apps[1].canvas), visible(apps[2].canvas))


class LossOptionTests(unittest.TestCase):
    def test_loss_rate_validation(self):
        for rate in (-0.1, 1.0, 2):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                network.MulticastPeer(lambda e: None, loss_rate=rate)

    def test_loss_rate_drops_a_seeded_fraction(self):
        net = SimNetwork()
        sender, _ = net.new_peer(peer_id=A)
        receiver, events = net.new_peer(peer_id=B, loss_rate=0.5, loss_seed=1)
        for x in range(200):
            sender.send({"type": "DRAW", "x": x % 50, "y": 0, "char": "#", "color": 1})
        net.deliver()
        self.assertEqual(receiver.simulated_drops + len(events.drain()), 200)
        self.assertTrue(70 < receiver.simulated_drops < 130)

    def test_cli_option(self):
        self.assertEqual(app.parse_args([]).simulate_loss, 0.0)
        self.assertEqual(app.parse_args(["--simulate-loss", "20"]).simulate_loss, 20.0)
        with mock.patch.object(network, "start_network") as start, \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(app.main(["--simulate-loss", "95"]), 1)
        self.assertIn("0 to 90", err.getvalue())
        start.assert_not_called()

    def test_cli_passes_loss_rate_to_network(self):
        with mock.patch.object(network, "start_network") as start, \
                mock.patch.object(network, "stop_network"), \
                mock.patch.object(app, "load_curses", return_value=mock.MagicMock()), \
                mock.patch.object(app.logging, "basicConfig"), \
                redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            start.return_value = mock.Mock(running=True, local_address=("192.0.2.1", 1),
                                           tracker=mock.Mock(totals=dict))
            app.main(["--simulate-loss", "20"])
        self.assertAlmostEqual(start.call_args.kwargs["loss_rate"], 0.2)


if __name__ == "__main__":
    unittest.main()
