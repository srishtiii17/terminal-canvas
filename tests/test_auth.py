import hashlib
import hmac
import io
import logging
import random
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import app
import auth
import network
import protocol
import protocol_v2
import sync
from app import CanvasApp
from canvas import Canvas
from protocol_v2 import AuthenticationError, decode_message, encode_message
from session import Session
from tests.simnet import SimNetwork
from tests.test_sync import FakeClock, visible

PASSPHRASE = "correct horse battery staple"
KEY = auth.derive_key(PASSPHRASE)            # Derived once: PBKDF2 is slow on purpose.
OTHER_KEY = auth.derive_key("a different passphrase")
A, B = 0xA, 0xB


def draw(**overrides):
    message = {"type": "DRAW", "peer": A, "seq": 3, "lamport": 4, "x": 1, "y": 2,
               "char": "#", "color": 5}
    message.update(overrides)
    return message


def flip(data, index):
    frame = bytearray(data)
    frame[index] ^= 0x01
    return bytes(frame)


class KeyDerivationTests(unittest.TestCase):
    def test_pbkdf2_sha256_with_fixed_salt(self):
        expected = hashlib.pbkdf2_hmac("sha256", PASSPHRASE.encode(), auth.SALT,
                                       auth.ITERATIONS, 32)
        self.assertEqual(KEY.tag(b"x"), hmac.new(expected, b"x", "sha256").digest()[:16])

    def test_same_passphrase_same_key_different_passphrase_different_key(self):
        self.assertEqual(auth.derive_key(PASSPHRASE, iterations=1000).tag(b"m"),
                         auth.derive_key(PASSPHRASE, iterations=1000).tag(b"m"))
        self.assertNotEqual(KEY.tag(b"m"), OTHER_KEY.tag(b"m"))

    def test_empty_passphrase_is_rejected(self):
        for value in ("", None, b"bytes"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                auth.derive_key(value)

    def test_key_is_never_shown(self):
        self.assertEqual(repr(KEY), "AuthKey(<hidden>)")
        self.assertEqual(str(KEY), "AuthKey(<hidden>)")
        self.assertFalse(hasattr(KEY, "__dict__"))

    def test_tag_is_truncated_hmac_sha256(self):
        self.assertEqual(len(KEY.tag(b"data")), auth.TAG_SIZE)
        self.assertEqual(auth.TAG_SIZE, 16)

    def test_verification_uses_compare_digest(self):
        with mock.patch.object(auth.hmac, "compare_digest", wraps=hmac.compare_digest) as cmp:
            self.assertTrue(KEY.verify(b"data", KEY.tag(b"data")))
            self.assertFalse(KEY.verify(b"data", bytes(16)))
        self.assertEqual(cmp.call_count, 2)


class AuthenticatedFrameTests(unittest.TestCase):
    def setUp(self):
        self.frame = encode_message(draw(), KEY)

    def test_correct_key_accepts(self):
        message = decode_message(self.frame, KEY)
        self.assertEqual((message["x"], message["authenticated"]), (1, True))
        self.assertEqual(self.frame[4] & protocol_v2.FLAG_AUTH, protocol_v2.FLAG_AUTH)
        self.assertEqual(len(self.frame), 26 + 16)
        self.assertEqual(self.frame[18:20], b"\x00\x06")  # Length excludes the tag.

    def test_incorrect_key_rejects(self):
        with self.assertRaisesRegex(AuthenticationError, "does not match"):
            decode_message(self.frame, OTHER_KEY)

    def test_modified_payload_rejects(self):
        for index in range(20, 26):
            with self.subTest(index=index), self.assertRaises(AuthenticationError):
                decode_message(flip(self.frame, index), KEY)

    def test_modified_header_rejects(self):
        # Every header byte is covered; some changes are caught earlier as
        # malformed (magic, version, flags, length), the rest by the tag.
        for index in range(20):
            with self.subTest(index=index), self.assertRaises(protocol.ProtocolError):
                decode_message(flip(self.frame, index), KEY)
        for index in (6, 10, 14):  # peer, sequence, lamport
            with self.subTest(index=index), self.assertRaisesRegex(AuthenticationError, "match"):
                decode_message(flip(self.frame, index), KEY)

    def test_modified_tag_rejects(self):
        for index in range(26, 42):
            with self.subTest(index=index), self.assertRaises(AuthenticationError):
                decode_message(flip(self.frame, index), KEY)

    def test_missing_tag_rejects(self):
        unauthenticated = encode_message(draw())
        with self.assertRaisesRegex(AuthenticationError, "no authentication tag"):
            decode_message(unauthenticated, KEY)
        stripped = self.frame[:-16]  # AUTH flag still set, tag removed.
        with self.assertRaisesRegex(AuthenticationError, "tag length"):
            decode_message(stripped, KEY)

    def test_invalid_tag_length_rejects(self):
        for data in (self.frame[:-1], self.frame + b"\x00", self.frame[:-8]):
            with self.subTest(size=len(data)):
                with self.assertRaisesRegex(AuthenticationError, "tag length"):
                    decode_message(data, KEY)

    def test_authentication_disabled_rejects_tagged_frames(self):
        with self.assertRaisesRegex(AuthenticationError, "no authentication key"):
            decode_message(self.frame)
        self.assertNotIn("authenticated", decode_message(encode_message(draw())))

    def test_every_message_type_can_be_authenticated(self):
        messages = [
            draw(), {"type": "CLEAR", "peer": A, "seq": 1},
            {"type": "STATE_REQ", "peer": A, "seq": 1, "nonce": 5},
            {**sync.make_chunks(Canvas(4, 2), 5)[0], "peer": A, "seq": 1},
            {"type": "HEARTBEAT", "peer": A, "seq": 1},
            {"type": "NACK", "peer": A, "seq": 1, "target": B, "first": 0, "count": 1},
            {"type": "NOT_AVAILABLE", "peer": A, "seq": 1, "first": 0, "count": 1, "oldest": 2},
            draw(retransmit=True),
        ]
        for message in messages:
            with self.subTest(type_=message["type"]):
                data = encode_message(message, KEY)
                self.assertEqual(decode_message(data, KEY)["type"], message["type"])
                with self.assertRaises(AuthenticationError):
                    decode_message(data, OTHER_KEY)

    def test_largest_authenticated_frame_fits_mtu(self):
        canvas = Canvas(60, 18)
        for y in range(18):
            for x in range(60):
                canvas.apply_event({"type": "DRAW", "x": x, "y": y, "char": "#", "color": 1,
                                    "lamport": 1, "peer": A})
        chunk = {**sync.make_chunks(canvas, 1)[0], "peer": A, "seq": 0}
        self.assertEqual(len(encode_message(chunk, KEY)), 1164 + 16)

    def test_v1_frame_in_authenticated_mode_is_legacy(self):
        v1 = protocol.encode_event({"type": "CLEAR", "seq": 0})
        with self.assertRaises(protocol_v2.LegacyFrameError):
            decode_message(v1, KEY)


class NetworkAuthTests(unittest.TestCase):
    def setUp(self):
        self.net = SimNetwork()

    def test_authentication_requires_v2(self):
        with self.assertRaises(ValueError):
            network.MulticastPeer(lambda e: None, wire_version=1, auth_key=KEY)

    def test_peers_with_same_key_communicate(self):
        a, _ = self.net.new_peer(peer_id=A, auth_key=KEY)
        b, events = self.net.new_peer(peer_id=B, auth_key=KEY)
        a.send({"type": "DRAW", "x": 1, "y": 1, "char": "#", "color": 1})
        self.net.deliver()
        self.assertEqual(len(events.drain()), 1)
        self.assertEqual(b.auth_failures, 0)

    def test_wrong_key_unauthenticated_and_tampered_frames_are_counted(self):
        receiver, events = self.net.new_peer(peer_id=B, auth_key=KEY)
        wrong, _ = self.net.new_peer(peer_id=0xC, auth_key=OTHER_KEY)
        plain, _ = self.net.new_peer(peer_id=0xD)
        for sender in (wrong, plain):
            sender.send({"type": "DRAW", "x": 1, "y": 1, "char": "#", "color": 1})
        self.net.inject(flip(encode_message(draw(peer=0xE), KEY), 21))
        self.net.deliver()
        self.assertEqual(events.drain(), [])
        self.assertEqual(receiver.auth_failures, 3)
        self.assertEqual(receiver.dropped_count, 0)

    def test_unauthenticated_v2_peers_still_work(self):
        a, _ = self.net.new_peer(peer_id=A)
        b, events = self.net.new_peer(peer_id=B)
        a.send({"type": "CLEAR"})
        self.net.deliver()
        self.assertEqual(events.drain()[0]["type"], "CLEAR")

    def test_v1_peers_are_unaffected(self):
        a, _ = self.net.new_peer(wire_version=1)
        b, events = self.net.new_peer(wire_version=1)
        a.send({"type": "DRAW", "x": 1, "y": 1, "char": "#", "color": 1})
        self.net.deliver()
        self.assertEqual(len(events.drain()), 1)
        self.assertEqual(len(self.net.sent[0][0]), 13)

    def test_key_and_passphrase_never_logged(self):
        receiver, _ = self.net.new_peer(peer_id=B, auth_key=KEY)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        self.net.inject(encode_message(draw(), OTHER_KEY))
        self.net.inject(flip(encode_message(draw(), KEY), 30))
        self.net.deliver()
        output = stream.getvalue()
        self.assertIn("unauthenticated", output)
        self.assertNotIn(PASSPHRASE, output)
        self.assertNotIn(KEY._key.hex(), output)


class SessionAuthTests(unittest.TestCase):
    """Late join and loss recovery still work with authentication on."""

    def setUp(self):
        self.net = SimNetwork(seed=5)
        self.clock = FakeClock()

    def make_app(self, peer_id, key=KEY):
        peer, recorder = self.net.new_peer(peer_id=peer_id, clock=self.clock, auth_key=key)
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

    def test_late_join_with_authentication(self):
        a = self.make_app(A)
        a.handle_key(" ")
        joiner = self.make_app(B)
        joiner.session.start()
        self.run_for([a, joiner], 0.6)
        self.assertEqual(joiner.session.sync.state, sync.SYNCED)
        self.assertEqual(visible(joiner.canvas), visible(a.canvas))

    def test_joiner_with_wrong_key_cannot_sync(self):
        a = self.make_app(A)
        a.handle_key(" ")
        joiner = self.make_app(B, key=OTHER_KEY)
        joiner.session.start()
        self.run_for([a, joiner], 4.0)
        self.assertEqual(joiner.session.sync.state, sync.GAVE_UP)
        self.assertGreater(joiner.peer.auth_failures, 0)
        self.assertIn("AuthFail", joiner.session.status())

    def test_recovery_with_authentication(self):
        a, b = self.make_app(A), self.make_app(B)
        self.run_for([a, b], 0.2)
        dropped = []

        def drop_first_draw(data, source, peer):
            if not dropped and data[3] == 1 and peer is b.peer:
                dropped.append(data)
                return True
            return False

        self.net.drop = drop_first_draw
        for x in range(3):
            a.cursor_x = x
            a.handle_key(" ")
        self.run_for([a, b], 1.0)
        self.assertEqual(len(dropped), 1)
        retransmitted = [d for d, _ in self.net.sent
                         if d[3] == 1 and d[4] & protocol_v2.FLAG_RETRANSMIT]
        self.assertEqual(len(retransmitted), 1)
        self.assertTrue(retransmitted[0][4] & protocol_v2.FLAG_AUTH)  # Re-signed.
        self.assertEqual(visible(b.canvas), visible(a.canvas))


class AuthOptionTests(unittest.TestCase):
    def setUp(self):
        self.start = mock.patch.object(network, "start_network").start()
        self.start.return_value = mock.Mock(
            running=True, local_address=("192.0.2.1", 1), peer_id=A,
            tracker=mock.Mock(totals=lambda: {"missing": 0, "duplicates": 0},
                              senders=lambda: []))
        mock.patch.object(network, "stop_network").start()
        mock.patch.object(app.logging, "basicConfig").start()
        mock.patch.object(app, "load_curses", return_value=mock.MagicMock()).start()
        # Fast key derivation for these CLI tests.
        real = auth.derive_key
        mock.patch.object(auth, "derive_key",
                          side_effect=lambda p: real(p, iterations=1000)).start()
        self.addCleanup(mock.patch.stopall)

    def run_main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(app.os.environ, env or {}, clear=False), \
                redirect_stdout(out), redirect_stderr(err):
            if env is None:
                app.os.environ.pop(app.PASSPHRASE_ENV, None)
            code = app.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_auth_is_off_by_default(self):
        self.assertFalse(app.parse_args([]).auth)
        self.run_main("--protocol", "2")
        self.assertIsNone(self.start.call_args.kwargs["auth_key"])

    def test_auth_needs_protocol_2(self):
        code, output = self.run_main("--auth")
        self.assertEqual(code, 1)
        self.assertIn("--protocol 2", output)
        self.start.assert_not_called()

    def test_passphrase_from_environment(self):
        with mock.patch.object(app.getpass, "getpass") as prompt:
            code, output = self.run_main("--protocol", "2", "--auth",
                                         env={app.PASSPHRASE_ENV: PASSPHRASE})
        prompt.assert_not_called()
        key = self.start.call_args.kwargs["auth_key"]
        self.assertEqual(key.tag(b"x"), auth.derive_key(PASSPHRASE).tag(b"x"))
        self.assertNotIn(PASSPHRASE, output)

    def test_passphrase_prompt_is_hidden(self):
        with mock.patch.object(app.getpass, "getpass", return_value=PASSPHRASE) as prompt:
            code, output = self.run_main("--protocol", "2", "--auth")
        prompt.assert_called_once()
        self.assertIsNotNone(self.start.call_args.kwargs["auth_key"])
        self.assertNotIn(PASSPHRASE, output)

    def test_empty_passphrase_exits(self):
        with mock.patch.object(app.getpass, "getpass", return_value=""):
            code, output = self.run_main("--protocol", "2", "--auth")
        self.assertEqual(code, 1)
        self.assertIn("must not be empty", output)
        self.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
