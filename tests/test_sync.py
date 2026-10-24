import queue
import random
import struct
import time
import unittest

import protocol_v2
import sync
from app import CanvasApp
from canvas import BLANK, DEFAULT_COLOR, NO_STAMP, Canvas
from protocol import ProtocolError
from session import Session
from sync import Reassembly, SnapshotError, Synchronizer, make_chunks
from network import MulticastPeer
from tests.simnet import SimNetwork
from tests.test_network_v2 import TEST_PORT, multicast_available

A, B, C = 0xA, 0xB, 0xC


def visible(canvas):
    return [(canvas.row_text(y), canvas.row_colors(y)) for y in range(canvas.height)]


def stamped_draw(lamport, peer, x, y, char="#", color=1):
    return {"type": "DRAW", "x": x, "y": y, "char": char, "color": color,
            "lamport": lamport, "peer": peer}


def full_canvas(width=60, height=18):
    canvas = Canvas(width, height)
    for y in range(height):
        for x in range(width):
            canvas.apply_event(stamped_draw(1 + x + y * width, A, x, y,
                                            chr(33 + (x * 7 + y) % 94), (x + y) % 8))
    return canvas


def wire(message, peer=B):
    """Round-trip a message through the v2 encoder, as the network would."""
    return {**protocol_v2.decode_message(protocol_v2.encode_message(
        {**message, "peer": peer, "seq": 0})), "addr": ("192.0.2.9", 1)}


class FixedDelays(random.Random):
    """A random source whose back-off delay is fixed, for deterministic tests."""

    def __init__(self, delay):
        super().__init__(0)
        self.delay = delay

    def uniform(self, a, b):
        return self.delay


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SyncProtocolTests(unittest.TestCase):
    def test_state_req_round_trip_and_size(self):
        data = protocol_v2.encode_message(
            {"type": "STATE_REQ", "peer": A, "seq": 3, "nonce": 0xDEADBEEF})
        self.assertEqual(len(data), 24)
        self.assertEqual(protocol_v2.decode_message(data)["nonce"], 0xDEADBEEF)

    def test_state_resp_round_trip(self):
        chunk = {"type": "STATE_RESP", "nonce": 7, "index": 1, "count": 3, "crc": 0xFFFFFFFF,
                 "width": 60, "height": 18, "clear_stamp": (4, C),
                 "cells": [(0, 0, "#", 1, 5, A), (59, 17, "~", 255, 2**32 - 1, B),
                           (3, 3, " ", 0, 9, A)]}
        data = protocol_v2.encode_message({**chunk, "peer": A, "seq": 0})
        self.assertEqual(len(data), 20 + 24 + 3 * 14)
        decoded = protocol_v2.decode_message(data)
        for key in ("nonce", "index", "count", "crc", "width", "height",
                    "clear_stamp", "cells"):
            self.assertEqual(decoded[key], chunk[key], key)

    def test_largest_chunk_fits_safe_udp_payload(self):
        chunks = make_chunks(full_canvas(), nonce=1)
        sizes = [len(protocol_v2.encode_message({**c, "peer": A, "seq": 0})) for c in chunks]
        self.assertEqual(max(sizes), 20 + 24 + 80 * 14)  # 1,164 bytes
        self.assertLess(max(sizes) + 16 + 8 + 20, 1500)    # + HMAC + UDP + IPv4 < MTU

    def test_malformed_state_messages_are_rejected(self):
        good = protocol_v2.encode_message({**make_chunks(full_canvas(), 1)[0], "peer": A, "seq": 0})
        header = bytearray(good[:20])

        def with_payload(payload):
            frame = bytearray(header)
            frame[18:20] = len(payload).to_bytes(2, "big")
            return bytes(frame) + payload

        chunk_header = good[20:44]
        cases = [
            (with_payload(chunk_header[:23]), "shorter than 24"),
            (with_payload(chunk_header + b"\x00" * 13), "multiple of 14"),
            (with_payload(chunk_header + good[44:44 + 14] * 81), "exceeds|more than 80"),
            (with_payload(struct.pack("!IHHIHHII", 1, 2, 2, 0, 60, 18, 0, 0)), "not below"),
            (with_payload(struct.pack("!IHHIHHII", 1, 0, 0, 0, 60, 18, 0, 0)), "not below"),
            (with_payload(struct.pack("!IHHIHHII", 1, 0, 1, 0, 0, 18, 0, 0)), "width"),
            (with_payload(chunk_header + struct.pack("!hhcBII", -1, 0, b"#", 0, 1, 1)), "x"),
            (with_payload(chunk_header + struct.pack("!hhcBII", 0, 0, b"\x01", 0, 1, 1)), "char"),
        ]
        for data, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ProtocolError, pattern):
                protocol_v2.decode_message(data)
        req = protocol_v2.encode_message({"type": "STATE_REQ", "peer": A, "seq": 0, "nonce": 1})
        bad_req = bytearray(req[:20])
        bad_req[18:20] = (5).to_bytes(2, "big")
        with self.assertRaisesRegex(ProtocolError, "STATE_REQ payload"):
            protocol_v2.decode_message(bytes(bad_req) + req[20:] + b"!")


class ChunkingTests(unittest.TestCase):
    def test_empty_canvas_is_one_empty_chunk(self):
        chunks = make_chunks(Canvas(60, 18), nonce=5)
        self.assertEqual(len(chunks), 1)
        self.assertEqual((chunks[0]["index"], chunks[0]["count"], chunks[0]["cells"]), (0, 1, []))
        self.assertEqual(chunks[0]["clear_stamp"], NO_STAMP)

    def test_full_canvas_needs_14_chunks(self):
        chunks = make_chunks(full_canvas(), nonce=5)
        self.assertEqual(len(chunks), 14)  # ceil(1080 / 80)
        self.assertEqual(sum(len(c["cells"]) for c in chunks), 60 * 18)
        self.assertEqual({c["crc"] for c in chunks}, {chunks[0]["crc"]})

    def test_snapshot_is_state_not_history(self):
        canvas = Canvas(10, 5)
        for lamport in range(1, 101):  # 100 edits of one cell...
            canvas.apply_event(stamped_draw(lamport, A, 2, 2, char="xo"[lamport % 2]))
        chunks = make_chunks(canvas, nonce=1)
        self.assertEqual(chunks[0]["cells"], [(2, 2, "x", 1, 100, A)])  # ...one record.

    def test_erased_cells_keep_their_stamp_in_the_snapshot(self):
        canvas = Canvas(10, 5)
        canvas.apply_event(stamped_draw(3, A, 1, 1, char=BLANK, color=DEFAULT_COLOR))
        self.assertEqual(make_chunks(canvas, 1)[0]["cells"], [(1, 1, " ", 0, 3, A)])


def chunks_on_wire(canvas, nonce, peer=B):
    return [wire(chunk, peer) for chunk in make_chunks(canvas, nonce)]


class ReassemblyTests(unittest.TestCase):
    def setUp(self):
        self.source = full_canvas(20, 10)  # 200 cells: 3 chunks.
        self.chunks = chunks_on_wire(self.source, nonce=9)

    def assemble(self, chunks):
        reassembly = Reassembly(chunks[0], 20, 10)
        done = False
        for chunk in chunks:
            done = reassembly.add(chunk)
        return reassembly, done

    def test_out_of_order_chunks_complete(self):
        reassembly, done = self.assemble([self.chunks[2], self.chunks[0], self.chunks[1]])
        self.assertTrue(done)
        clear_stamp, cells = reassembly.result()
        self.assertEqual(cells, self.source.stamped_cells())

    def test_missing_chunk_is_incomplete(self):
        reassembly, done = self.assemble([self.chunks[0], self.chunks[2]])
        self.assertFalse(done)
        self.assertEqual(reassembly.missing, 1)

    def test_duplicate_chunk_is_ignored(self):
        reassembly, done = self.assemble([self.chunks[0], self.chunks[0], self.chunks[1],
                                          self.chunks[2]])
        self.assertTrue(done)
        self.assertEqual(reassembly.duplicates, 1)

    def test_conflicting_copy_of_a_chunk_is_rejected(self):
        altered = {**self.chunks[1], "cells": self.chunks[1]["cells"][:-1]}
        with self.assertRaisesRegex(SnapshotError, "two different copies"):
            self.assemble([self.chunks[1], altered])

    def test_chunk_from_another_snapshot_is_rejected(self):
        other = chunks_on_wire(Canvas(20, 10), nonce=9)[0]  # Different CRC and count.
        with self.assertRaisesRegex(SnapshotError, "same snapshot"):
            self.assemble([self.chunks[0], other])

    def test_invalid_crc_is_rejected(self):
        tampered = [dict(c) for c in self.chunks]
        cells = list(tampered[1]["cells"])
        x, y, char, color, lamport, peer = cells[0]
        cells[0] = (x, y, "!" if char != "!" else "?", color, lamport, peer)
        tampered[1]["cells"] = cells
        reassembly, done = self.assemble(tampered)
        self.assertTrue(done)
        with self.assertRaisesRegex(SnapshotError, "CRC32"):
            reassembly.result()

    def test_invalid_dimensions_are_rejected(self):
        with self.assertRaisesRegex(SnapshotError, "20x10 canvas, this one is 60x18"):
            Reassembly(self.chunks[0], 60, 18)

    def test_out_of_bounds_or_repeated_cells_are_rejected(self):
        for cells, pattern in (([(25, 0, "#", 1, 1, A)], "outside"),
                               ([(1, 1, "#", 1, 1, A), (1, 1, "*", 1, 2, A)], "repeats")):
            chunk = wire({"type": "STATE_RESP", "nonce": 1, "index": 0, "count": 1,
                          "crc": sync.snapshot_crc(20, 10, NO_STAMP, cells),
                          "width": 20, "height": 10, "clear_stamp": NO_STAMP, "cells": cells})
            reassembly = Reassembly(chunk, 20, 10)
            reassembly.add(chunk)
            with self.subTest(pattern=pattern), self.assertRaisesRegex(SnapshotError, pattern):
                reassembly.result()


class SynchronizerTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.canvas = Canvas(20, 10)
        self.sync = Synchronizer(self.canvas, self.sent.append, random.Random(3))

    def test_request_sends_state_req_with_random_nonce(self):
        self.sync.request(now=0)
        self.assertEqual(self.sent, [{"type": "STATE_REQ", "nonce": self.sync.nonce}])
        self.assertTrue(self.sync.waiting)

    def test_response_for_another_nonce_is_ignored(self):
        self.sync.request(now=0)
        for chunk in chunks_on_wire(full_canvas(20, 10), nonce=self.sync.nonce ^ 1):
            self.assertIsNone(self.sync.on_response(chunk, now=0.1))
        self.assertTrue(self.sync.waiting)

    def test_complete_response_returns_snapshot(self):
        self.sync.request(now=0)
        result = None
        for chunk in chunks_on_wire(full_canvas(20, 10), nonce=self.sync.nonce):
            result = self.sync.on_response(chunk, now=0.1)
        clear_stamp, cells, responder = result
        self.assertEqual((len(cells), responder), (200, B))
        self.assertEqual(self.sync.state, sync.SYNCED)

    def test_second_responder_is_ignored(self):
        self.sync.request(now=0)
        nonce = self.sync.nonce
        first = chunks_on_wire(full_canvas(20, 10), nonce, peer=B)
        second = chunks_on_wire(Canvas(20, 10), nonce, peer=C)
        self.sync.on_response(first[0], now=0.1)
        self.assertIsNone(self.sync.on_response(second[0], now=0.1))
        self.assertEqual(self.sync.stats["ignored_chunks"], 1)
        for chunk in first[1:]:
            result = self.sync.on_response(chunk, now=0.1)
        self.assertEqual(result[2], B)

    def test_timeout_retries_with_new_nonce_then_gives_up(self):
        self.sync.request(now=0)
        nonces = [self.sync.nonce]
        self.assertIsNone(self.sync.tick(now=0.9))
        self.assertIsNone(self.sync.tick(now=1.0))      # Attempt 2.
        nonces.append(self.sync.nonce)
        self.assertIsNone(self.sync.tick(now=2.0))      # Attempt 3.
        nonces.append(self.sync.nonce)
        self.assertEqual(self.sync.tick(now=3.0), sync.GAVE_UP)
        self.assertEqual(len(set(nonces)), 3)
        self.assertEqual(len(self.sent), sync.MAX_ATTEMPTS)
        self.assertEqual(self.sync.tick(now=10.0), None)  # No further retries.
        self.assertEqual(len(self.sent), sync.MAX_ATTEMPTS)

    def test_incomplete_snapshot_times_out_and_is_retried(self):
        self.sync.request(now=0)
        chunks = chunks_on_wire(full_canvas(20, 10), nonce=self.sync.nonce)
        self.sync.on_response(chunks[0], now=0.1)          # Chunks 1 and 2 lost.
        self.sync.tick(now=1.0)
        self.assertIn("2 chunk(s) missing", self.sync.last_error)
        self.assertEqual(self.sync.attempts, 2)

    def test_invalid_snapshot_is_dropped_then_retried(self):
        self.sync.request(now=0)
        for chunk in chunks_on_wire(full_canvas(30, 10), nonce=self.sync.nonce):
            self.assertIsNone(self.sync.on_response(chunk, now=0.1))
        self.assertEqual(self.sync.stats["rejected"], 1)
        self.assertIn("30x10", self.sync.last_error)
        self.sync.tick(now=1.0)
        self.assertEqual(self.sync.attempts, 2)

    def test_responder_waits_random_delay_then_sends_chunks(self):
        self.canvas.apply_event(stamped_draw(1, A, 1, 1))
        self.sync.on_request({"nonce": 77, "peer": C}, now=0)
        due = self.sync.pending[77]
        self.assertTrue(0 <= due <= 0.2)
        self.sync.tick(now=due - 0.001)
        self.assertEqual(self.sent, [])
        self.sync.tick(now=due)
        self.assertEqual([m["type"] for m in self.sent], ["STATE_RESP"])
        self.assertEqual(self.sent[0]["cells"], [(1, 1, "#", 1, 1, A)])
        self.sync.on_request({"nonce": 77, "peer": C}, now=1)  # Same request again.
        self.sync.tick(now=5)
        self.assertEqual(len(self.sent), 1)

    def test_responder_cancels_when_another_peer_answers_first(self):
        self.sync.on_request({"nonce": 77, "peer": C}, now=0)
        other = chunks_on_wire(Canvas(20, 10), nonce=77, peer=B)[0]
        self.sync.on_response(other, now=0.05)
        self.sync.tick(now=1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.sync.stats["suppressed"], 1)

    def test_peer_that_is_still_waiting_answers_later(self):
        # Found with two real processes: two peers that start within a few
        # seconds of each other must not both refuse to answer.
        self.sync.request(now=0)
        self.sync.on_request({"nonce": 5, "peer": C}, now=0)
        self.assertTrue(0.2 <= self.sync.pending[5] <= 0.4)

    def test_two_peers_starting_together_exchange_canvases(self):
        other_sent = []
        other = Synchronizer(Canvas(20, 10), other_sent.append, random.Random(4))
        self.canvas.apply_event(stamped_draw(1, A, 1, 1))
        self.sync.request(now=0)
        other.request(now=0)
        other.on_request(wire(self.sent[0], peer=A), now=0)
        other.tick(now=0.5)
        result = None
        for chunk in other_sent[1:]:
            result = self.sync.on_response(wire(chunk, peer=B), now=0.5)
        self.assertIsNotNone(result)
        self.assertEqual(self.sync.state, sync.SYNCED)


class MergeTests(unittest.TestCase):
    def test_snapshot_never_overwrites_newer_events(self):
        source = Canvas(10, 5)
        source.apply_event(stamped_draw(5, A, 1, 1, char="o"))   # Older.
        source.apply_event(stamped_draw(5, A, 2, 2, char="k"))
        joiner = Canvas(10, 5)
        joiner.apply_event(stamped_draw(9, C, 1, 1, char="n"))   # Newer, seen live.
        sync.merge_snapshot(joiner, source.clear_stamp, source.stamped_cells())
        self.assertEqual(joiner.cell(1, 1)[0], "n")
        self.assertEqual(joiner.cell(2, 2)[0], "k")

    def test_snapshot_clear_stamp_erases_older_local_cells_only(self):
        source = Canvas(10, 5)
        source.apply_event({"type": "CLEAR", "lamport": 6, "peer": A})
        joiner = Canvas(10, 5)
        joiner.apply_event(stamped_draw(3, B, 0, 0, char="o"))
        joiner.apply_event(stamped_draw(8, B, 1, 0, char="n"))
        sync.merge_snapshot(joiner, source.clear_stamp, source.stamped_cells())
        self.assertEqual(joiner.cell(0, 0)[0], BLANK)
        self.assertEqual(joiner.cell(1, 0)[0], "n")

    def test_unstamped_snapshot_cells_fill_only_blank_cells(self):
        joiner = Canvas(10, 5)
        joiner.apply_event(stamped_draw(1, B, 0, 0, char="s"))
        sync.merge_snapshot(joiner, NO_STAMP, [(0, 0, "u", 2, 0, 0), (1, 0, "u", 2, 0, 0)])
        self.assertEqual(joiner.cell(0, 0)[0], "s")
        self.assertEqual(joiner.cell(1, 0), ("u", 2))

    def test_merge_order_does_not_matter(self):
        source = full_canvas(10, 5)
        live = [stamped_draw(100 + i, C, i % 10, i % 5, char="L") for i in range(20)]
        live.append({"type": "CLEAR", "lamport": 30, "peer": B})
        results = set()
        rng = random.Random(5)
        for _ in range(50):
            canvas = Canvas(10, 5)
            events = list(live)
            rng.shuffle(events)
            split = rng.randrange(len(events) + 1)
            for event in events[:split]:
                canvas.apply_event(event)
            sync.merge_snapshot(canvas, source.clear_stamp, source.stamped_cells())
            for event in events[split:]:
                canvas.apply_event(event)
            results.add(repr(visible(canvas)))
        self.assertEqual(len(results), 1)


class SessionSyncTests(unittest.TestCase):
    """Sessions over the in-memory v2 network with a fake clock."""

    def setUp(self):
        self.net = SimNetwork(seed=11)
        self.clock = FakeClock()
        self.rng = random.Random(11)

    def make_app(self, peer_id, width=20, height=10):
        peer, recorder = self.net.new_peer(peer_id=peer_id)
        canvas = Canvas(width, height)
        session = Session(canvas, peer, time_source=self.clock, rng=self.rng)
        return CanvasApp(canvas, peer.send, recorder.events, session=session)

    def run_for(self, apps, seconds, step=0.05):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.net.deliver()
            for application in apps:
                application.process_remote_events()
                application.tick()
            self.clock.now += step
        self.net.deliver()
        for application in apps:
            application.process_remote_events()

    def test_joiner_receives_existing_canvas(self):
        a = self.make_app(A)
        for key in (" ", "RIGHT", "b", "@", " ", "DOWN", "3", " "):
            a.handle_key(key)
        self.run_for([a], 0.1)
        joiner = self.make_app(B)
        joiner.session.start()
        self.run_for([a, joiner], 0.5)
        self.assertEqual(visible(joiner.canvas), visible(a.canvas))
        self.assertIn("synchronised from peer 0000000a", joiner.message)
        self.assertEqual(joiner.session.status(), "Synced")
        # The joiner's clock moved past the snapshot, so its edits win.
        joiner.handle_key(" ")
        self.run_for([a, joiner], 0.1)
        self.assertEqual(visible(joiner.canvas), visible(a.canvas))
        self.assertEqual(a.canvas.cell(0, 0)[0], "#")
        self.assertEqual(a.canvas.cell_stamp(0, 0)[1], B)  # B's newer write won.

    def test_full_canvas_transfer_uses_multiple_chunks(self):
        a = self.make_app(A, 60, 18)
        for y in range(18):
            for x in range(60):
                a.canvas.apply_event(a.session.stamp(
                    {"type": "DRAW", "x": x, "y": y, "char": "%", "color": y % 8}))
        joiner = self.make_app(B, 60, 18)
        joiner.session.start()
        self.run_for([a, joiner], 0.5)
        self.assertEqual(visible(joiner.canvas), visible(a.canvas))
        responses = [d for d, _ in self.net.sent if d[3] == 4]
        self.assertEqual(len(responses), 14)

    def responders(self):
        return [protocol_v2.decode_message(d)["peer"] for d, _ in self.net.sent
                if d[3] == 4 and protocol_v2.decode_message(d)["index"] == 0]

    def joiner_with_responders(self, delays):
        peers = [self.make_app(p) for p in (A, C, 0xD)]
        for application, delay in zip(peers, delays):
            application.session.sync.rng = FixedDelays(delay)
        peers[0].handle_key(" ")
        self.run_for(peers, 0.1)
        joiner = self.make_app(B)
        joiner.session.start()
        self.run_for(peers + [joiner], 0.6)
        return peers, joiner

    def test_first_answer_suppresses_the_other_responders(self):
        peers, joiner = self.joiner_with_responders([0.15, 0.02, 0.18])
        self.assertEqual(self.responders(), [C])  # The shortest back-off.
        self.assertEqual(sum(p.session.sync.stats["suppressed"] for p in peers), 2)
        self.assertEqual(visible(joiner.canvas), visible(peers[0].canvas))

    def test_simultaneous_answers_are_harmless(self):
        # Suppression is best-effort: peers whose timers fire in the same
        # loop iteration both answer. The joiner keeps the first responder.
        peers, joiner = self.joiner_with_responders([0.1, 0.1, 0.18])
        self.assertEqual(sorted(self.responders()), [A, C])
        # The first complete snapshot is merged once; the second is ignored.
        self.assertEqual(joiner.session.sync.state, sync.SYNCED)
        self.assertEqual(joiner.session.sync.stats["requests_sent"], 1)
        self.assertEqual(joiner.message.count("synchronised"), 1)
        self.assertEqual(visible(joiner.canvas), visible(peers[0].canvas))

    def test_no_existing_peer_gives_up_and_keeps_canvas(self):
        alone = self.make_app(A)
        alone.session.start()
        alone.handle_key(" ")  # Local drawing during the wait is kept.
        self.run_for([alone], 4.0)
        self.assertEqual(alone.session.sync.state, sync.GAVE_UP)
        self.assertIn("gave up (no peer answered)", alone.message)
        self.assertEqual(alone.canvas.cell(0, 0)[0], "#")
        requests = [d for d, _ in self.net.sent if d[3] == 3]
        self.assertEqual(len(requests), sync.MAX_ATTEMPTS)

    def test_live_events_during_sync_are_buffered_then_merged(self):
        a = self.make_app(A)
        a.handle_key(" ")
        self.run_for([a], 0.1)
        joiner = self.make_app(B)
        joiner.session.start()
        self.net.deliver()
        a.process_remote_events()             # A has the request...
        a.handle_key("RIGHT")
        a.handle_key(" ")                     # ...and draws before answering.
        self.net.deliver()
        joiner.process_remote_events()
        self.assertEqual(len(joiner.session.buffer), 1)
        self.assertEqual(joiner.canvas.cell(1, 0)[0], BLANK)  # Held, not applied.
        self.run_for([a, joiner], 0.5)
        self.assertEqual(joiner.session.buffer, [])
        self.assertEqual(visible(joiner.canvas), visible(a.canvas))

    def test_lost_snapshot_chunk_is_recovered_by_retry(self):
        a = self.make_app(A)
        a.handle_key(" ")
        self.run_for([a], 0.1)
        joiner = self.make_app(B)
        dropped = []

        def drop_first_response(data, source, peer):
            if data[3] == 4 and peer.peer_id == B and not dropped:
                dropped.append(data)
                return True
            return False

        self.net.drop = drop_first_response
        joiner.session.start()
        self.run_for([a, joiner], 2.0)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(joiner.session.sync.attempts, 2)
        self.assertEqual(visible(joiner.canvas), visible(a.canvas))

    def test_network_down_during_sync_is_not_fatal(self):
        alone = self.make_app(A)
        self.net.down = True
        with self.assertLogs("session", level="WARNING"):
            alone.session.start()
            self.run_for([alone], 4.0)
        # Every STATE_REQ (and, since loss recovery, every HEARTBEAT) failed.
        self.assertEqual(alone.session.sync.stats["requests_sent"], sync.MAX_ATTEMPTS)
        self.assertGreaterEqual(alone.session.send_errors, sync.MAX_ATTEMPTS)
        self.assertEqual(alone.session.sync.state, sync.GAVE_UP)


@unittest.skipUnless(
    multicast_available(TEST_PORT), "IPv4 multicast loopback unavailable on this host"
)
class LoopbackLateJoinTests(unittest.TestCase):
    """Real v2 sockets on this host: a second app joins after drawing exists."""

    def make_app(self):
        inbox = queue.Queue()
        peer = MulticastPeer(inbox.put, port=TEST_PORT, wire_version=2)
        peer.start()
        self.addCleanup(peer.stop)
        canvas = Canvas(20, 10)
        return CanvasApp(canvas, peer.send, inbox, session=Session(canvas, peer))

    def test_late_joiner_receives_canvas_over_real_multicast(self):
        first = self.make_app()
        for key in (" ", "RIGHT", "RIGHT", "5", " "):
            first.handle_key(key)
        joiner = self.make_app()
        joiner.session.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and joiner.session.sync.state == sync.WAITING:
            for application in (first, joiner):
                application.process_remote_events()
                application.tick()
            time.sleep(0.01)
        self.assertEqual(joiner.session.sync.state, sync.SYNCED)
        self.assertEqual(visible(joiner.canvas), visible(first.canvas))


if __name__ == "__main__":
    unittest.main()
