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
import protocol_v2
from network import MulticastPeer
from tests.simnet import Recorder, SimNetwork

OTHER = ("192.0.2.2", 40001)


def draw(**overrides):
    event = {"type": "DRAW", "x": 20, "y": 10, "char": "#", "color": 2}
    event.update(overrides)
    return event


def v2(peer=0x0BADF00D, **overrides):
    return protocol_v2.encode_message({**draw(), "peer": peer, "seq": 0, **overrides})


class V2HandleDatagramTests(unittest.TestCase):
    def setUp(self):
        self.recorder = Recorder()
        self.peer = MulticastPeer(self.recorder, wire_version=2, peer_id=0x11111111)
        self.peer.local_address = ("192.0.2.1", 40000)

    def test_rejects_unknown_wire_version(self):
        for version in (0, 3, "2"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                MulticastPeer(self.recorder, wire_version=version)

    def test_peer_id_is_random_when_not_given(self):
        with mock.patch.object(protocol_v2, "new_peer_id", return_value=0x42) as new_id:
            peer = MulticastPeer(self.recorder, wire_version=2)
        self.assertEqual(peer.peer_id, 0x42)
        new_id.assert_called_once()

    def test_delivers_v2_event_with_peer_identity(self):
        event = self.peer.handle_datagram(v2(seq=3, lamport=9), OTHER)
        self.assertEqual(event, {**draw(), "peer": 0x0BADF00D, "seq": 3, "lamport": 9,
                                 "retransmit": False, "addr": OTHER})
        self.assertEqual(self.recorder.drain(), [event])

    def test_ignores_own_frames_by_address(self):
        self.assertIsNone(self.peer.handle_datagram(v2(peer=0x11111111), self.peer.local_address))
        self.assertEqual(self.recorder.drain(), [])
        self.assertEqual(self.peer.peer_id, 0x11111111)

    def test_v1_frame_is_dropped_and_counted_as_legacy(self):
        v1 = protocol.encode_event({**draw(), "seq": 0})
        with self.assertLogs("network", level="WARNING") as logs:
            for _ in range(3):
                self.assertIsNone(self.peer.handle_datagram(v1, OTHER))
        self.assertEqual(len(logs.records), 1)  # Warned once per sender address.
        self.assertEqual((self.peer.legacy_count, self.peer.dropped_count), (3, 0))

    def test_malformed_datagrams_are_dropped_and_counted(self):
        good = v2()
        for data in (b"", b"junk", good[:-1], good + b"!", b"\xca\x55" + b"\x00" * 30):
            self.assertIsNone(self.peer.handle_datagram(data, OTHER))
        self.assertEqual(self.peer.dropped_count, 5)
        self.assertEqual(self.recorder.drain(), [])

    def test_duplicates_are_dropped_per_peer_id(self):
        frame = v2(seq=4)
        self.assertIsNotNone(self.peer.handle_datagram(frame, OTHER))
        # The same peer ID from a new source port is still the same sender.
        self.assertIsNone(self.peer.handle_datagram(frame, ("192.0.2.2", 40099)))
        self.assertEqual(self.peer.tracker.stats(0x0BADF00D).duplicates, 1)

    def test_independent_peers_with_same_address(self):
        # Two peer IDs behind one address are two senders.
        self.assertIsNotNone(self.peer.handle_datagram(v2(peer=0xA, seq=0), OTHER))
        self.assertIsNotNone(self.peer.handle_datagram(v2(peer=0xB, seq=0), OTHER))
        self.assertEqual(self.peer.tracker.totals()["senders"], 2)
        self.assertEqual(self.peer.tracker.totals()["duplicates"], 0)

    def test_gap_and_late_arrival_by_peer_id(self):
        for seq in (0, 3, 1):
            self.assertIsNotNone(self.peer.handle_datagram(v2(seq=seq), OTHER))
        stats = self.peer.tracker.stats(0x0BADF00D)
        self.assertEqual((stats.gaps, stats.late, stats.missing), (2, 1, 1))

    def test_sequence_wraparound(self):
        for seq in (2**32 - 2, 2**32 - 1, 0, 1):
            self.assertIsNotNone(self.peer.handle_datagram(v2(seq=seq), OTHER))
        self.assertIsNone(self.peer.handle_datagram(v2(seq=2**32 - 1), OTHER))
        stats = self.peer.tracker.stats(0x0BADF00D)
        self.assertEqual((stats.received, stats.gaps, stats.duplicates), (4, 0, 1))

    def test_late_duplicate_is_dropped_even_after_silence(self):
        # v1 treats a repeat after 2 s of silence as a restarted sender; v2
        # has peer IDs for that, so a stale duplicate stays a duplicate.
        clock = [0.0]
        self.peer.tracker._clock = lambda: clock[0]
        self.peer.handle_datagram(v2(seq=5), OTHER)
        clock[0] = 60.0
        self.assertIsNone(self.peer.handle_datagram(v2(seq=5), OTHER))

    def test_peer_id_collision_picks_new_id_and_keeps_the_event(self):
        with self.assertLogs("network", level="WARNING"):
            event = self.peer.handle_datagram(v2(peer=0x11111111), OTHER)
        self.assertIsNotNone(event)
        self.assertNotEqual(self.peer.peer_id, 0x11111111)
        self.assertEqual(self.peer.peer_id_changes, 1)


class V2SendTests(unittest.TestCase):
    def setUp(self):
        self.net = SimNetwork()
        self.peer, _ = self.net.new_peer(peer_id=0x22222222)

    def sent(self):
        return [protocol_v2.decode_message(data) for data, _ in self.net.sent]

    def test_send_assigns_sequence_and_peer_id(self):
        self.assertEqual(self.peer.send(draw()), 0)
        self.assertEqual(self.peer.send({"type": "CLEAR"}), 1)
        sent = self.sent()
        self.assertEqual([m["seq"] for m in sent], [0, 1])
        self.assertTrue(all(m["peer"] == 0x22222222 for m in sent))
        self.assertEqual([len(d) for d, _ in self.net.sent], [26, 20])

    def test_send_ignores_caller_peer_and_seq_and_keeps_lamport(self):
        self.peer.send(draw(peer=5, seq=99, lamport=12))
        self.assertEqual(self.sent()[0]["peer"], 0x22222222)
        self.assertEqual(self.sent()[0]["seq"], 0)
        self.assertEqual(self.sent()[0]["lamport"], 12)

    def test_send_rejects_non_data_types_without_consuming_sequence(self):
        for event in ({"type": "STATE_REQ"}, {"type": "HEARTBEAT"}, draw(x=-1)):
            with self.subTest(event=event), self.assertRaises(protocol.ProtocolError):
                self.peer.send(event)
        self.assertEqual(self.peer.send(draw()), 0)

    def test_sequence_wraps(self):
        self.peer._sequence = protocol.MAX_SEQUENCE
        self.assertEqual(self.peer.send(draw()), protocol.MAX_SEQUENCE)
        self.assertEqual(self.peer.send(draw()), 0)

    def test_v1_peer_drops_v2_frames(self):
        v1_peer, v1_events = self.net.new_peer(wire_version=1)
        self.peer.send(draw())
        self.net.deliver()
        self.assertEqual(v1_events.drain(), [])
        self.assertEqual(v1_peer.dropped_count, 1)


class SimulatedV2PeersTests(unittest.TestCase):
    """Several v2 peers on the in-memory network, through the real receive path."""

    def setUp(self):
        self.net = SimNetwork(seed=1)
        self.a, self.a_events = self.net.new_peer()
        self.b, self.b_events = self.net.new_peer()
        self.c, self.c_events = self.net.new_peer()

    def test_events_reach_every_other_peer_once(self):
        self.a.send(draw(x=1))
        self.b.send(draw(x=2))
        self.net.deliver()
        self.assertEqual([e["x"] for e in self.a_events.drain()], [2])
        self.assertEqual([e["x"] for e in self.b_events.drain()], [1])
        self.assertEqual(sorted(e["x"] for e in self.c_events.drain()), [1, 2])

    def test_events_carry_sender_peer_id(self):
        self.a.send(draw())
        self.net.deliver()
        self.assertEqual(self.b_events.drain()[0]["peer"], self.a.peer_id)

    def test_duplicated_and_reordered_delivery(self):
        self.a.send(draw(x=0))  # First contact sets the sender's baseline.
        self.net.deliver()
        for x in range(1, 10):
            self.a.send(draw(x=x))
        self.net.deliver(shuffle=True, duplicate=True)
        received = [e["x"] for e in self.b_events.drain()]
        self.assertEqual(sorted(received), list(range(10)))
        self.assertEqual(self.b.tracker.stats(self.a.peer_id).duplicates, 9)

    def test_frames_older_than_first_contact_are_not_delivered(self):
        # Known limitation, as in v1: the first frame heard from a peer is its
        # baseline, so an earlier frame reordered behind it counts as old.
        for x in range(3):
            self.a.send(draw(x=x))
        self.net.in_flight.reverse()
        self.net.deliver()
        self.assertEqual([e["x"] for e in self.b_events.drain()], [2])

    def test_dropped_frame_is_counted_missing(self):
        for x in range(5):
            self.a.send(draw(x=x))
        self.net.drop = lambda data, source, peer: (
            peer is self.b and protocol_v2.decode_message(data)["seq"] == 2)
        self.net.deliver()
        self.assertEqual(self.b.tracker.stats(self.a.peer_id).missing, 1)
        self.assertEqual(self.c.tracker.stats(self.a.peer_id).missing, 0)

    def test_foreign_datagrams_are_dropped(self):
        self.net.inject(b"hello")
        self.net.inject(protocol.encode_event({**draw(), "seq": 0}))
        with self.assertLogs("network", level="WARNING"):
            self.net.deliver()
        self.assertEqual(self.b_events.drain(), [])
        self.assertEqual((self.b.dropped_count, self.b.legacy_count), (1, 1))


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
class V2LoopbackMulticastTests(unittest.TestCase):
    """Real sockets on this host (skipped if multicast loopback is unavailable)."""

    def setUp(self):
        self.alice_inbox, self.bob_inbox = Recorder(), Recorder()
        self.alice = MulticastPeer(self.alice_inbox, port=TEST_PORT, wire_version=2)
        self.bob = MulticastPeer(self.bob_inbox, port=TEST_PORT, wire_version=2)
        for peer in (self.alice, self.bob):
            peer.start()
            self.addCleanup(peer.stop)

    def test_v2_events_flow_both_ways_with_peer_ids(self):
        self.alice.send(draw(x=1))
        self.bob.send(draw(x=2))
        got_b = self.bob_inbox.events.get(timeout=2)
        got_a = self.alice_inbox.events.get(timeout=2)
        self.assertEqual((got_b["x"], got_b["peer"]), (1, self.alice.peer_id))
        self.assertEqual((got_a["x"], got_a["peer"]), (2, self.bob.peer_id))
        time.sleep(0.1)
        self.assertTrue(self.alice_inbox.events.empty())  # No echo of own frames.


class ProtocolOptionTests(unittest.TestCase):
    def setUp(self):
        self.start = mock.patch.object(network, "start_network").start()
        self.start.return_value = mock.Mock(
            running=True, local_address=("192.0.2.1", 1), peer_id=0xABCDEF01,
            tracker=mock.Mock(totals=lambda: {"missing": 0, "duplicates": 0},
                              senders=lambda: []))
        mock.patch.object(network, "stop_network").start()
        mock.patch.object(app.logging, "basicConfig").start()
        self.addCleanup(mock.patch.stopall)
        self.curses = mock.MagicMock(name="curses")
        mock.patch.object(app, "load_curses", return_value=self.curses).start()

    def run_main(self, *argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = app.main(list(argv))
        return code, err.getvalue()

    def test_default_is_v1(self):
        self.assertEqual(app.parse_args([]).protocol, 1)
        self.run_main()
        self.assertEqual(self.start.call_args.kwargs["wire_version"], 1)

    def test_protocol_2_is_passed_to_network(self):
        self.assertEqual(app.parse_args(["--protocol", "2"]).protocol, 2)
        self.run_main("--protocol", "2")
        self.assertEqual(self.start.call_args.kwargs["wire_version"], 2)

    def test_invalid_protocol_is_rejected(self):
        for value in ("3", "0", "v2"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                with redirect_stderr(io.StringIO()):
                    app.parse_args(["--protocol", value])


if __name__ == "__main__":
    unittest.main()
