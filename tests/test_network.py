import errno
import queue
import random
import socket
import struct
import time
import unittest
from unittest import mock

import config
import network
import protocol
from network import MulticastPeer, start_network, send_event, stop_network
from protocol import CLEAR, DRAW


def draw(**overrides):
    event = {"type": DRAW, "x": 20, "y": 10, "char": "#", "color": 2}
    event.update(overrides)
    return event


class Recorder:
    """Event handler that records everything it receives."""

    def __init__(self):
        self.events = queue.Queue()

    def __call__(self, event):
        self.events.put(event)

    def get(self, timeout=2.0):
        return self.events.get(timeout=timeout)

    def empty(self):
        return self.events.empty()


# ---------------------------------------------------------------------------
# Unit tests: receive-path logic, no sockets at all.
# ---------------------------------------------------------------------------


class HandleDatagramTests(unittest.TestCase):
    SELF = ("192.0.2.1", 40000)
    OTHER = ("192.0.2.2", 40001)

    def setUp(self):
        self.recorder = Recorder()
        self.peer = MulticastPeer(self.recorder)
        self.peer.local_address = self.SELF

    def test_delivers_events_from_other_peers_with_sender_address(self):
        data = protocol.encode_event(draw(seq=7))
        event = self.peer.handle_datagram(data, self.OTHER)
        expected = {**draw(seq=7), "addr": self.OTHER}
        self.assertEqual(event, expected)
        self.assertEqual(self.recorder.get(timeout=0), expected)

    def test_ignores_own_frames(self):
        data = protocol.encode_event({"type": CLEAR, "seq": 0})
        self.assertIsNone(self.peer.handle_datagram(data, self.SELF))
        self.assertTrue(self.recorder.empty())
        self.assertEqual(self.peer.dropped_count, 0)

    def test_same_host_different_port_is_another_peer(self):
        data = protocol.encode_event({"type": CLEAR, "seq": 0})
        other_port = (self.SELF[0], self.SELF[1] + 1)
        self.assertIsNotNone(self.peer.handle_datagram(data, other_port))

    def test_drops_and_counts_invalid_datagrams(self):
        valid = protocol.encode_event(draw(seq=0))
        reserved = struct.pack(
            protocol.FRAME_FORMAT, protocol.MAGIC, protocol.MessageType.STATE_REQ,
            0, 0, 0, 0, b"\x00",
        )
        for data in (b"", b"garbage", valid[:-1], valid + b"!", b"\x00" * 13, reserved):
            self.assertIsNone(self.peer.handle_datagram(data, self.OTHER))
        self.assertTrue(self.recorder.empty())
        self.assertEqual(self.peer.dropped_count, 6)

    def test_duplicate_frame_from_same_sender_is_dropped(self):
        data = protocol.encode_event(draw(seq=4))
        self.assertIsNotNone(self.peer.handle_datagram(data, self.OTHER))
        self.assertIsNone(self.peer.handle_datagram(data, self.OTHER))
        self.assertEqual(self.recorder.events.qsize(), 1)
        self.assertEqual(self.peer.tracker.totals()["duplicates"], 1)
        self.assertEqual(self.peer.dropped_count, 0)  # Duplicates are not malformed.

    def test_duplicate_clear_cannot_replay(self):
        clear = protocol.encode_event({"type": CLEAR, "seq": 0})
        later = protocol.encode_event(draw(seq=1))
        for data in (clear, later, clear):
            self.peer.handle_datagram(data, self.OTHER)
        delivered = [self.recorder.get(timeout=0)["type"] for _ in range(2)]
        self.assertEqual(delivered, [CLEAR, DRAW])
        self.assertTrue(self.recorder.empty())

    def test_same_sequence_from_different_senders_is_not_duplicate(self):
        data = protocol.encode_event(draw(seq=0))
        self.assertIsNotNone(self.peer.handle_datagram(data, self.OTHER))
        self.assertIsNotNone(self.peer.handle_datagram(data, ("192.0.2.3", 40001)))
        self.assertEqual(self.peer.tracker.totals()["duplicates"], 0)

    def test_gap_is_counted_and_late_frame_still_delivered(self):
        for seq in (0, 3, 1):
            self.assertIsNotNone(
                self.peer.handle_datagram(protocol.encode_event(draw(seq=seq)), self.OTHER))
        totals = self.peer.tracker.totals()
        self.assertEqual((totals["gaps"], totals["late"], totals["missing"]), (2, 1, 1))

    def test_handler_exception_is_logged_not_raised(self):
        def broken_handler(event):
            raise RuntimeError("boom")

        peer = MulticastPeer(broken_handler)
        data = protocol.encode_event({"type": CLEAR, "seq": 0})
        with self.assertLogs("network", level="ERROR"):
            self.assertIsNotNone(peer.handle_datagram(data, self.OTHER))


class PeerStateTests(unittest.TestCase):
    def test_defaults_match_presentation(self):
        self.assertEqual(config.MULTICAST_GROUP, "239.255.0.1")
        self.assertEqual(config.MULTICAST_PORT, 5005)
        peer = MulticastPeer(Recorder())
        self.assertEqual((peer.group, peer.port), ("239.255.0.1", 5005))

    def test_send_before_start_raises(self):
        with self.assertRaises(RuntimeError):
            MulticastPeer(Recorder()).send(draw())

    def test_stop_without_start_is_harmless(self):
        peer = MulticastPeer(Recorder())
        peer.stop()
        peer.stop()
        self.assertFalse(peer.running)

    def test_module_api_requires_start(self):
        stop_network()
        with self.assertRaises(RuntimeError):
            send_event(draw())
        stop_network()  # Idempotent.


# ---------------------------------------------------------------------------
# Mocked-socket tests: verify socket options without touching the network.
# ---------------------------------------------------------------------------


def scripted_recvfrom(*results):
    """Fake recvfrom: returns or raises each result in turn, then times out."""
    remaining = iter(results)

    def recvfrom(bufsize):
        try:
            result = next(remaining)
        except StopIteration:
            time.sleep(0.01)  # Keep the idle receive loop from spinning.
            raise socket.timeout
        if isinstance(result, BaseException):
            raise result
        return result

    return recvfrom


class MockedSocketTests(unittest.TestCase):
    # Pinned rather than read from config.py, so these tests do not depend on
    # the interface configured on the machine running them.
    INTERFACE = "0.0.0.0"
    MREQ = socket.inet_aton("239.255.0.1") + socket.inet_aton(INTERFACE)
    SELF = ("192.0.2.1", 40000)
    OTHER = ("192.0.2.2", 40001)

    def setUp(self):
        patcher = mock.patch.object(network.socket, "socket")
        self.socket_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.recv_sock = mock.MagicMock(name="recv_sock")
        self.recv_sock.recvfrom.side_effect = scripted_recvfrom()
        self.send_sock = mock.MagicMock(name="send_sock")
        self.send_sock.getsockname.return_value = self.SELF
        self.socket_class.side_effect = [self.recv_sock, self.send_sock]
        self.recorder = Recorder()
        self.peer = MulticastPeer(self.recorder, interface=self.INTERFACE)
        self.addCleanup(self.peer.stop)

    def test_default_interface_comes_from_config(self):
        self.assertEqual(MulticastPeer(Recorder()).interface, config.INTERFACE_ADDRESS)

    def test_outgoing_interface_selection(self):
        self.peer.start()
        if self.INTERFACE == "0.0.0.0":
            # The OS picks the outgoing route.
            self.send_sock.bind.assert_not_called()
            for call in self.send_sock.setsockopt.call_args_list:
                self.assertNotEqual(call.args[1], socket.IP_MULTICAST_IF)
        else:
            self.send_sock.setsockopt.assert_any_call(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.INTERFACE),
            )
            self.send_sock.bind.assert_called_once_with((self.INTERFACE, 0))
            # Bind must happen before connect() picks a route.
            names = [c[0] for c in self.send_sock.mock_calls]
            self.assertLess(names.index("bind"), names.index("connect"))

    def test_receive_socket_binds_and_joins_group(self):
        self.peer.start()
        self.recv_sock.bind.assert_called_once_with(("", 5005))
        self.recv_sock.setsockopt.assert_any_call(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, self.MREQ
        )
        self.recv_sock.setsockopt.assert_any_call(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
        )

    def test_send_socket_sets_ttl_and_connects_to_group(self):
        self.peer.start()
        self.send_sock.setsockopt.assert_any_call(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1
        )
        self.send_sock.setsockopt.assert_any_call(
            socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1
        )
        self.send_sock.connect.assert_called_once_with(("239.255.0.1", 5005))
        self.assertEqual(self.peer.local_address, self.SELF)

    def test_receive_loop_delivers_and_filters(self):
        own = protocol.encode_event({"type": CLEAR, "seq": 0})
        other = protocol.encode_event(draw(seq=3))
        self.recv_sock.recvfrom.side_effect = scripted_recvfrom(
            (own, self.SELF), (b"junk", self.OTHER), (other, self.OTHER)
        )
        self.peer.start()
        self.assertEqual(self.recorder.get(), {**draw(seq=3), "addr": self.OTHER})
        self.assertEqual(self.peer.dropped_count, 1)
        self.recv_sock.recvfrom.assert_called_with(config.RECV_BUFFER_SIZE)

    def test_receive_loop_survives_oversized_datagram_error(self):
        too_big = OSError(errno.EMSGSIZE, "message too long")
        frame = protocol.encode_event({"type": CLEAR, "seq": 0})
        self.recv_sock.recvfrom.side_effect = scripted_recvfrom(
            too_big, ConnectionResetError(), (frame, self.OTHER)
        )
        self.peer.start()
        self.assertEqual(self.recorder.get()["type"], CLEAR)
        self.assertTrue(self.peer.running)
        self.assertEqual(self.peer.dropped_count, 1)

    def test_unexpected_receive_error_ends_thread_and_is_logged(self):
        self.recv_sock.recvfrom.side_effect = scripted_recvfrom(
            OSError(errno.EBADF, "bad file descriptor")
        )
        with self.assertLogs("network", level="ERROR"):
            self.peer.start()
            self.peer._thread.join(timeout=2)
        self.assertFalse(self.peer.running)
        self.peer.stop()  # Still cleans up sockets.
        self.recv_sock.close.assert_called_once()
        self.send_sock.close.assert_called_once()

    def test_send_writes_frame_and_assigns_increasing_sequence(self):
        self.peer.start()
        self.assertEqual(self.peer.send(draw(seq=1234)), 0)  # Caller seq replaced.
        self.assertEqual(self.peer.send({"type": CLEAR}), 1)
        frames = [c.args[0] for c in self.send_sock.send.call_args_list]
        self.assertEqual(
            [protocol.decode_event(f) for f in frames],
            [draw(seq=0), {"type": CLEAR, "seq": 1}],
        )

    def test_send_does_not_modify_caller_event(self):
        self.peer.start()
        event = draw()
        self.peer.send(event)
        self.assertEqual(event, draw())

    def test_invalid_event_raises_and_does_not_consume_sequence(self):
        self.peer.start()
        with self.assertRaises(protocol.ProtocolError):
            self.peer.send(draw(x=-5))
        self.send_sock.send.assert_not_called()
        self.assertEqual(self.peer.send(draw()), 0)

    def test_sequence_wraps(self):
        self.peer.start()
        self.peer._sequence = protocol.MAX_SEQUENCE
        self.assertEqual(self.peer.send(draw()), protocol.MAX_SEQUENCE)
        self.assertEqual(self.peer.send(draw()), 0)

    def test_stop_leaves_group_and_closes_sockets(self):
        self.peer.start()
        self.peer.stop()
        self.recv_sock.setsockopt.assert_any_call(
            socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, self.MREQ
        )
        self.recv_sock.close.assert_called_once()
        self.send_sock.close.assert_called_once()
        self.assertFalse(self.peer.running)
        self.assertIsNone(self.peer.local_address)

    def test_start_twice_raises(self):
        self.peer.start()
        with self.assertRaises(RuntimeError):
            self.peer.start()

    def test_send_socket_failure_closes_receive_socket(self):
        self.send_sock.connect.side_effect = OSError("no route")
        with self.assertRaises(OSError):
            self.peer.start()
        self.recv_sock.close.assert_called_once()
        self.send_sock.close.assert_called_once()
        self.assertFalse(self.peer.running)

    def test_receive_thread_stops_on_stop(self):
        self.peer.start()
        self.assertTrue(self.peer.running)
        thread = self.peer._thread
        self.peer.stop()
        self.assertFalse(thread.is_alive())


class MockedSpecificInterfaceTests(MockedSocketTests):
    """Every mocked test again, with a specific interface address.

    Joining and leaving the group then name that interface (via MREQ), and
    outgoing traffic is pinned to it with IP_MULTICAST_IF and bind().
    """

    INTERFACE = "192.0.2.50"
    MREQ = socket.inet_aton("239.255.0.1") + socket.inet_aton(INTERFACE)


# ---------------------------------------------------------------------------
# Real multicast on this host: two peers exchange frames via OS loopback.
# ---------------------------------------------------------------------------


def multicast_available(port):
    """True if this host can join a multicast group and loop traffic back."""
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


# A random high port keeps test runs from colliding with a real session.
TEST_PORT = random.randint(40000, 49999)


@unittest.skipUnless(
    multicast_available(TEST_PORT), "IPv4 multicast loopback unavailable on this host"
)
class LoopbackMulticastTests(unittest.TestCase):
    def setUp(self):
        self.alice_inbox = Recorder()
        self.bob_inbox = Recorder()
        self.alice = MulticastPeer(self.alice_inbox, port=TEST_PORT)
        self.bob = MulticastPeer(self.bob_inbox, port=TEST_PORT)
        self.alice.start()
        self.addCleanup(self.alice.stop)
        self.bob.start()
        self.addCleanup(self.bob.stop)

    def test_events_reach_other_peer(self):
        self.alice.send(draw())
        self.alice.send({"type": CLEAR})

        first = self.bob_inbox.get()
        self.assertEqual(first, {**draw(seq=0), "addr": self.alice.local_address})
        second = self.bob_inbox.get()
        self.assertEqual(second, {"type": CLEAR, "seq": 1, "addr": self.alice.local_address})

    def test_events_flow_both_ways_and_are_not_echoed(self):
        self.alice.send({"type": CLEAR})
        self.bob.send(draw(char="@"))

        self.assertEqual(self.bob_inbox.get()["type"], CLEAR)
        self.assertEqual(self.alice_inbox.get()["char"], "@")
        with self.assertRaises(queue.Empty):
            self.alice_inbox.get(timeout=0.5)
        with self.assertRaises(queue.Empty):
            self.bob_inbox.get(timeout=0.1)

    def test_foreign_datagrams_are_dropped(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"not a canvas frame", (self.bob.group, TEST_PORT))
        self.alice.send({"type": CLEAR})

        self.assertEqual(self.bob_inbox.get()["type"], CLEAR)
        self.assertGreaterEqual(self.bob.dropped_count, 1)

    def test_resent_frame_is_dropped_as_duplicate(self):
        # Re-send the identical frame from alice's own socket, so it has the
        # same source address and sequence number: a real network duplicate.
        frame = protocol.encode_event({"type": CLEAR, "seq": 0})
        self.alice._send_socket.send(frame)
        self.alice._send_socket.send(frame)
        self.alice.send(draw())  # seq 0 from alice's counter: also a duplicate.
        self.alice._send_socket.send(protocol.encode_event({**draw(), "seq": 1}))

        self.assertEqual(self.bob_inbox.get()["type"], CLEAR)
        self.assertEqual(self.bob_inbox.get()["seq"], 1)
        with self.assertRaises(queue.Empty):
            self.bob_inbox.get(timeout=0.5)
        self.assertEqual(self.bob.tracker.totals()["duplicates"], 2)

    def test_oversized_datagram_does_not_stop_receiver(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"x" * 3000, (self.bob.group, TEST_PORT))
        self.alice.send({"type": CLEAR})

        self.assertEqual(self.bob_inbox.get()["type"], CLEAR)
        self.assertTrue(self.bob.running)
        self.assertGreaterEqual(self.bob.dropped_count, 1)

    def test_no_callbacks_after_stop(self):
        self.bob.stop()
        self.alice.send(draw())
        with self.assertRaises(queue.Empty):
            self.bob_inbox.get(timeout=0.5)

    def test_restart_after_stop(self):
        self.bob.stop()
        self.bob.start()
        self.alice.send({"type": CLEAR})
        self.assertEqual(self.bob_inbox.get()["type"], CLEAR)

    def test_handler_exception_does_not_stop_receiving(self):
        calls = queue.Queue()

        def flaky_handler(event):
            calls.put(event)
            if event["seq"] == 0:
                raise RuntimeError("boom")

        carol = MulticastPeer(flaky_handler, port=TEST_PORT)
        carol.start()
        self.addCleanup(carol.stop)
        with self.assertLogs("network", level="ERROR"):
            self.alice.send(draw())
            self.alice.send(draw())
            # The second event is handled only after the first one's error
            # was logged, so both are captured once it arrives.
            self.assertEqual(calls.get(timeout=2)["seq"], 0)
            self.assertEqual(calls.get(timeout=2)["seq"], 1)

    def test_module_level_api(self):
        inbox = Recorder()
        start_network(inbox, port=TEST_PORT)
        self.addCleanup(stop_network)
        with self.assertRaises(RuntimeError):
            start_network(inbox, port=TEST_PORT)

        self.alice.send(draw(char="A"))
        self.assertEqual(inbox.get()["char"], "A")
        self.assertEqual(send_event(draw(char="B")), 0)
        self.assertEqual(self.alice_inbox.get()["char"], "B")

        stop_network()
        with self.assertRaises(RuntimeError):
            send_event(draw())


if __name__ == "__main__":
    unittest.main()
