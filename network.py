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

Most callers use the module-level API::

    start_network(on_event)   # join the group and start receiving
    send_event(event)         # multicast a DRAW or CLEAR event dict
    stop_network()            # stop receiving, leave the group, close sockets
"""

from __future__ import annotations

import errno
import logging
import socket
import struct
import threading
from typing import Callable, Optional

import config
import protocol
from tracker import SequenceTracker, Verdict

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict], None]


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
    """

    def __init__(
        self,
        on_event: EventHandler,
        group: str = config.MULTICAST_GROUP,
        port: int = config.MULTICAST_PORT,
        interface: str = config.INTERFACE_ADDRESS,
        ttl: int = config.MULTICAST_TTL,
        loopback: bool = config.MULTICAST_LOOPBACK,
    ) -> None:
        self.group = group
        self.port = port
        self.interface = interface
        self.ttl = ttl
        self.loopback = loopback
        self.dropped_count = 0
        self.local_address: Optional[tuple] = None
        self.tracker = SequenceTracker()

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
            frame = protocol.encode_event({**event, "seq": sequence})
            self._sequence = (sequence + 1) % (protocol.MAX_SEQUENCE + 1)
        send_socket.send(frame)
        return sequence

    def handle_datagram(self, data: bytes, address: tuple) -> Optional[dict]:
        """Decode one datagram and dispatch it; returns the event if delivered."""
        if address == self.local_address:
            return None  # Our own frame, looped back by the OS.
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
        try:
            self._on_event(event)
        except Exception:
            # A faulty handler must not kill the receive thread.
            logger.exception("event handler raised")
        return event

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
    loopback); defaults come from config.py. Raises RuntimeError if the
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
    """Multicast a DRAW or CLEAR event dict; returns its sequence number."""
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
