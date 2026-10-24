# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast
# Implementation and development by Srishti Saluja
"""UDP/IPv4 multicast transport for terminal canvas events.

Each peer opens two sockets:

* a receive socket bound to the multicast port that joins the group with
  IP_ADD_MEMBERSHIP (the OS then sends IGMP membership reports), and leaves
  it with IP_DROP_MEMBERSHIP on shutdown;
* a send socket on an ephemeral port, connected to the group. Its local
  (address, port) is unique per peer, which lets the receiver recognise and
  ignore its own frames when the OS loops multicast back to this host.

A background thread receives frames, decodes them and calls ``on_event``.
There is no server: every peer is both sender and receiver.

``wire_version`` selects the frame format: 1 (the default) is the 13-byte
v1 frame of protocol.py; 2 is the versioned protocol_v2 format, which
carries a random per-session peer ID. In v2 mode senders are tracked by
peer ID instead of source address, and v1 frames are dropped and counted.

For loss recovery (v2) each peer keeps its last HISTORY_SIZE sent events
and answers a NACK for its own events on the receive thread: it multicasts
the original event again with the RETRANSMIT flag, at most once per event
per RETRANSMIT_INTERVAL, or NOT_AVAILABLE for events it no longer has.
The receiving side (when to NACK, retries) is recovery.py.

With an `auth_key` (auth.py, v2 only) every frame sent is HMAC-tagged and
only frames with a valid tag are accepted; failures are counted in
`auth_failures`. The key is never logged.

Most callers use the module-level API::

    start_network(on_event)   # join the group and start receiving
    send_event(event)         # multicast a DRAW or CLEAR event dict
    stop_network()            # stop receiving, leave the group, close sockets
"""

from __future__ import annotations

import errno
import logging
import math
import random
import socket
import struct
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional

import config
import protocol
import protocol_v2
from tracker import SequenceTracker, Verdict

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict], None]

# Sent v2 events kept for retransmission.
HISTORY_SIZE = 512

# A NACKed event (or NOT_AVAILABLE range) is answered at most this often.
RETRANSMIT_INTERVAL = 0.2

# Sequence numbers answered per NACK, so one request cannot trigger a flood.
MAX_NACK_COUNT = 256


def _membership_request(group: str, interface: str) -> bytes:
    return struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(interface))


def create_receive_socket(
    group: str = config.MULTICAST_GROUP,
    port: int = config.MULTICAST_PORT,
    interface: str = config.INTERFACE_ADDRESS,
    loopback: bool = config.MULTICAST_LOOPBACK,
) -> socket.socket:
    """Create a UDP socket bound to `port` and joined to `group`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        # Allow several peers on the same host to share the port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass  # Defined but unsupported on some kernels.

        # Windows cannot bind to a multicast address, so bind to all
        # interfaces; stray datagrams are rejected by decode_event().
        sock.bind(("", port))
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            _membership_request(group, interface),
        )
        # Windows applies loopback on the receiving socket, others on the
        # sending one, so set it on both.
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1 if loopback else 0
        )
    except OSError:
        sock.close()
        raise
    return sock


def create_send_socket(
    group: str = config.MULTICAST_GROUP,
    port: int = config.MULTICAST_PORT,
    interface: str = config.INTERFACE_ADDRESS,
    ttl: int = config.MULTICAST_TTL,
    loopback: bool = config.MULTICAST_LOOPBACK,
) -> socket.socket:
    """Create a UDP socket connected to the group, ready to multicast."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1 if loopback else 0
        )
        if interface != "0.0.0.0":
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface)
            )
            sock.bind((interface, 0))
        # Connecting fixes the outgoing interface address and an ephemeral
        # port, so getsockname() is the source address receivers will see.
        sock.connect((group, port))
    except OSError:
        sock.close()
        raise
    return sock


class MulticastPeer:
    """Sends and receives canvas events over a multicast group.

    `on_event(event)` is called on the receive thread for every valid frame
    sent by another peer. The event dict is the one returned by
    protocol.decode_event() plus an "addr" key holding the sender's
    (ip, port). Frames this peer sent itself, malformed frames and duplicate
    frames (per-sender sequence numbers already received) are dropped;
    `tracker` counts duplicates and sequence gaps per sender.

    With wire_version=2 the event also has "peer", "lamport" and
    "retransmit" keys, `tracker` is keyed by peer ID, and `peer_id` is this
    peer's own ID. If another address sends with our peer ID, this peer
    picks a new one (counted in `peer_id_changes`).

    `loss_rate` (0 to 1) drops that fraction of received datagrams on
    purpose, counted in `simulated_drops`, to demonstrate recovery on one
    laptop. `clock` is used for retransmission rate limits. `auth_key`
    (an auth.AuthKey, v2 only) enables HMAC authentication.
    """

    def __init__(
        self,
        on_event: EventHandler,
        group: str = config.MULTICAST_GROUP,
        port: int = config.MULTICAST_PORT,
        interface: str = config.INTERFACE_ADDRESS,
        ttl: int = config.MULTICAST_TTL,
        loopback: bool = config.MULTICAST_LOOPBACK,
        wire_version: int = 1,
        peer_id: Optional[int] = None,
        history_size: int = HISTORY_SIZE,
        loss_rate: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        loss_seed: Optional[int] = None,
        auth_key=None,
    ) -> None:
        if auth_key is not None and wire_version != 2:
            raise ValueError("authentication needs wire_version=2")
        if not 0.0 <= loss_rate < 1.0:
            raise ValueError(f"loss_rate must be from 0 to below 1, got {loss_rate!r}")
        if wire_version not in (1, 2):
            raise ValueError(f"wire_version must be 1 or 2, got {wire_version!r}")
        self.group = group
        self.port = port
        self.interface = interface
        self.ttl = ttl
        self.loopback = loopback
        self.dropped_count = 0
        self.local_address: Optional[tuple] = None
        self.wire_version = wire_version
        if wire_version == 1:
            self.tracker = SequenceTracker()
        else:
            # A restarted v2 peer has a new peer ID, so the idle-restart
            # heuristic (meant for reused source ports) is not needed and
            # would let a late duplicate through.
            self.tracker = SequenceTracker(idle_reset_seconds=math.inf)
        self.peer_id = peer_id if peer_id is not None else protocol_v2.new_peer_id()
        protocol_v2.check_int("peer_id", self.peer_id, protocol_v2.MAX_U32, minimum=1)
        self.peer_id_changes = 0
        self.legacy_count = 0  # v1 frames dropped in v2 mode.
        self._legacy_senders: set = set()

        self.loss_rate = loss_rate
        self.simulated_drops = 0
        self._loss_rng = random.Random(loss_seed)
        self._clock = clock
        self.history_size = history_size
        self._history: "OrderedDict[int, dict]" = OrderedDict()  # seq -> event
        self._last_answer: dict = {}  # seq or (first, count) -> time answered
        self.retransmitted = 0
        self.not_available_sent = 0
        self.auth_key = auth_key
        self.auth_failures = 0

        self._on_event = on_event
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._recv_socket: Optional[socket.socket] = None
        self._send_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._recv_socket is not None:
            raise RuntimeError("peer already started")
        recv_socket = create_receive_socket(
            self.group, self.port, self.interface, self.loopback
        )
        try:
            send_socket = create_send_socket(
                self.group, self.port, self.interface, self.ttl, self.loopback
            )
        except OSError:
            recv_socket.close()
            raise
        recv_socket.settimeout(config.SOCKET_TIMEOUT)
        self.tracker.reset()
        self._recv_socket = recv_socket
        self._send_socket = send_socket
        self.local_address = send_socket.getsockname()
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._receive_loop, name="multicast-receiver", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop receiving, leave the group and close both sockets.

        Safe to call more than once or before start(). Once it returns,
        on_event will not be called again.
        """
        self._stopping.set()
        if self._thread is not None:
            if self._thread is not threading.current_thread():
                self._thread.join()
            self._thread = None
        if self._recv_socket is not None:
            try:
                self._recv_socket.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_DROP_MEMBERSHIP,
                    _membership_request(self.group, self.interface),
                )
            except OSError:
                logger.debug("leaving multicast group failed", exc_info=True)
            self._recv_socket.close()
            self._recv_socket = None
        if self._send_socket is not None:
            self._send_socket.close()
            self._send_socket = None
        self.local_address = None

    def __enter__(self) -> "MulticastPeer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def send(self, event: dict) -> int:
        """Multicast an event and return the sequence number assigned to it.

        The peer owns the sequence counter, so any "seq" in `event` is
        replaced. `event` itself is not modified. Raises ProtocolError for an
        invalid event, in which case no sequence number is consumed.
        """
        send_socket = self._send_socket
        if send_socket is None:
            raise RuntimeError("peer not started")
        with self._sequence_lock:
            sequence = self._sequence
            # Encode before consuming the number so invalid events leave no gap.
            stored = {**event, "seq": sequence}
            frame = self._encode_data(stored)
            self._sequence = (sequence + 1) % (protocol.MAX_SEQUENCE + 1)
            if self.wire_version == 2:
                self._history[sequence] = stored
                while len(self._history) > self.history_size:
                    self._history.popitem(last=False)
        send_socket.send(frame)
        return sequence

    def send_control(self, message: dict) -> None:
        """Multicast a v2 control message such as STATE_REQ (v2 only).

        Control messages carry this peer's next sequence number but do not
        consume it. Raises ProtocolError for DRAW/CLEAR or an invalid message.
        """
        send_socket = self._send_socket
        if send_socket is None:
            raise RuntimeError("peer not started")
        if self.wire_version != 2:
            raise RuntimeError("control messages need wire_version=2")
        if message.get("type") in protocol_v2.DATA_TYPES:
            raise protocol.ProtocolError("use send() for DRAW and CLEAR")
        with self._sequence_lock:
            frame = protocol_v2.encode_message(
                {**message, "peer": self.peer_id, "seq": self._sequence}, self.auth_key)
        send_socket.send(frame)

    def _encode_data(self, event: dict) -> bytes:
        if self.wire_version == 1:
            return protocol.encode_event(event)
        if isinstance(event, dict) and event.get("type") not in protocol_v2.DATA_TYPES:
            raise protocol.ProtocolError(f"send() takes DRAW or CLEAR, got {event.get('type')!r}")
        return protocol_v2.encode_message({**event, "peer": self.peer_id}, self.auth_key)

    def handle_datagram(self, data: bytes, address: tuple) -> Optional[dict]:
        """Decode one datagram and dispatch it; returns the event if delivered."""
        if address == self.local_address:
            return None  # Our own frame, looped back by the OS.
        if self.loss_rate and self._loss_rng.random() < self.loss_rate:
            self.simulated_drops += 1
            return None
        if self.wire_version == 2:
            return self._handle_v2(data, address)
        try:
            event = protocol.decode_event(data)
        except protocol.ProtocolError as exc:
            self.dropped_count += 1
            logger.debug("dropped invalid datagram from %s: %s", address, exc)
            return None
        if self.tracker.observe(address, event["seq"]) is Verdict.DUPLICATE:
            logger.debug("dropped duplicate seq %d from %s", event["seq"], address)
            return None
        event["addr"] = address
        return self._dispatch(event)

    def _dispatch(self, event: dict) -> dict:
        try:
            self._on_event(event)
        except Exception:
            # A faulty handler must not kill the receive thread.
            logger.exception("event handler raised")
        return event

    def _handle_v2(self, data: bytes, address: tuple) -> Optional[dict]:
        try:
            message = protocol_v2.decode_message(data, self.auth_key)
        except protocol_v2.AuthenticationError as exc:
            self.auth_failures += 1
            logger.debug("dropped unauthenticated datagram from %s: %s", address, exc)
            return None
        except protocol_v2.LegacyFrameError:
            self.legacy_count += 1
            if address not in self._legacy_senders:
                self._legacy_senders.add(address)
                logger.warning("dropping v1 frames from %s: this peer runs protocol v2", address)
            return None
        except protocol.ProtocolError as exc:
            self.dropped_count += 1
            logger.debug("dropped invalid datagram from %s: %s", address, exc)
            return None
        if message["peer"] == self.peer_id:
            # Another address uses our ID: keep its message, take a new ID.
            self._change_peer_id()
        kind = message["type"]
        if kind in protocol_v2.DATA_TYPES:
            if self.tracker.observe(message["peer"], message["seq"]) is Verdict.DUPLICATE:
                logger.debug("dropped duplicate seq %d from peer %08x",
                             message["seq"], message["peer"])
                return None
        elif kind == "HEARTBEAT":
            self.tracker.observe_heartbeat(message["peer"], message["seq"])
        elif kind == "NACK" and message["target"] == self.peer_id:
            self._answer_nack(message["first"], message["count"])
        message["addr"] = address
        return self._dispatch(message)

    def _answer_nack(self, first: int, count: int) -> None:
        """Retransmit NACKed events we still have; NOT_AVAILABLE for the rest."""
        now = self._clock()
        resend, unavailable = [], []
        with self._sequence_lock:
            for offset in range(min(count, MAX_NACK_COUNT)):
                seq = (first + offset) % (protocol.MAX_SEQUENCE + 1)
                event = self._history.get(seq)
                if event is None:
                    unavailable.append(seq)
                elif now - self._last_answer.get(seq, -math.inf) >= RETRANSMIT_INTERVAL:
                    self._last_answer[seq] = now
                    resend.append(self._encode_data({**event, "retransmit": True}))
            oldest = next(iter(self._history), self._sequence)
            for key in [k for k in self._last_answer
                        if not isinstance(k, tuple) and k not in self._history]:
                del self._last_answer[key]  # Keep the rate-limit table bounded.
            report = None
            if unavailable:
                key = (unavailable[0], len(unavailable))
                if now - self._last_answer.get(key, -math.inf) >= RETRANSMIT_INTERVAL:
                    self._last_answer[key] = now
                    report = {"type": "NOT_AVAILABLE", "first": unavailable[0],
                              "count": len(unavailable), "oldest": oldest}
        send_socket = self._send_socket
        if send_socket is None:
            return
        try:
            for frame in resend:
                send_socket.send(frame)
                self.retransmitted += 1
            if report is not None:
                self.send_control(report)
                self.not_available_sent += 1
        except OSError as exc:
            logger.warning("could not answer NACK: %s", exc)

    def send_heartbeat(self) -> None:
        """Multicast a HEARTBEAT carrying our next sequence number (v2)."""
        self.send_control({"type": "HEARTBEAT"})

    def _change_peer_id(self) -> None:
        with self._sequence_lock:
            old = self.peer_id
            while self.peer_id == old:
                self.peer_id = protocol_v2.new_peer_id()
            self.peer_id_changes += 1
        logger.warning("peer ID %08x is used by another peer; now using %08x", old, self.peer_id)

    def _receive_loop(self) -> None:
        sock = self._recv_socket
        assert sock is not None
        while not self._stopping.is_set():
            try:
                data, address = sock.recvfrom(config.RECV_BUFFER_SIZE)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows reports ICMP errors from earlier sends here; ignore.
                continue
            except OSError as exc:
                if self._stopping.is_set():
                    break
                if exc.errno == errno.EMSGSIZE:
                    # Datagram larger than the buffer; Windows raises here.
                    self.dropped_count += 1
                    logger.debug("dropped oversized datagram")
                    continue
                logger.exception("receive failed; receiver thread exiting")
                break
            if self._stopping.is_set():
                break
            self.handle_datagram(data, address)


_peer: Optional[MulticastPeer] = None
_peer_lock = threading.Lock()


def start_network(on_event: EventHandler, **options) -> MulticastPeer:
    """Join the multicast group and start delivering events to `on_event`.

    `options` are passed to MulticastPeer (group, port, interface, ttl,
    loopback, wire_version, peer_id, history_size, loss_rate, ...);
    defaults come from config.py. Raises RuntimeError if the
    network is already started and OSError if the sockets cannot be set up.
    """
    global _peer
    with _peer_lock:
        if _peer is not None:
            raise RuntimeError("network already started")
        peer = MulticastPeer(on_event, **options)
        peer.start()
        _peer = peer
        return peer


def send_event(event: dict) -> int:
    """Multicast a DRAW or CLEAR event dict; returns its sequence number.

    The frame format is the one the network was started with (v1 by default).
    """
    peer = _peer
    if peer is None:
        raise RuntimeError("network not started")
    return peer.send(event)


def stop_network() -> None:
    """Stop the network started by start_network(). Safe to call repeatedly."""
    global _peer
    with _peer_lock:
        peer, _peer = _peer, None
    if peer is not None:
        peer.stop()
