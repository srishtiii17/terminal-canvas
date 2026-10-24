"""In-memory multicast network for tests: no sockets, deterministic delivery.

Several MulticastPeer objects are attached to one SimNetwork. A peer's
send() goes through a fake socket into the network's in-flight list;
deliver() hands every in-flight datagram to every attached peer's
handle_datagram(), exactly as the receive thread would. The sender gets its
own copy too, as with multicast loopback, and must ignore it.

Loss, reordering and duplication are controlled by the test:

    net.drop = lambda data, source, peer: ...   # True drops that copy
    net.deliver(shuffle=True)                    # seeded random order
    net.deliver(duplicate=True)                  # every copy twice
    net.down = True                              # send() raises OSError
"""

from __future__ import annotations

import errno
import queue
import random

import network


class Recorder:
    """Event handler that records everything it receives."""

    def __init__(self):
        self.events = queue.Queue()

    def __call__(self, event):
        self.events.put(event)

    def drain(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                return items


class FakeSendSocket:
    def __init__(self, net, peer):
        self.net = net
        self.peer = peer

    def send(self, data):
        self.net.transmit(data, self.peer.local_address)
        return len(data)

    def close(self):
        pass


class SimNetwork:
    def __init__(self, seed=0):
        self.peers = []
        self.in_flight = []
        self.sent = []  # Every datagram transmitted: (data, source).
        self.rng = random.Random(seed)
        self.drop = None
        self.down = False

    def add_peer(self, peer):
        index = len(self.peers) + 1
        peer.local_address = (f"192.0.2.{index}", 50000 + index)
        peer._send_socket = FakeSendSocket(self, peer)
        self.peers.append(peer)
        return peer

    def new_peer(self, wire_version=2, **options):
        recorder = Recorder()
        peer = network.MulticastPeer(recorder, wire_version=wire_version, **options)
        self.add_peer(peer)
        return peer, recorder

    def transmit(self, data, source):
        if self.down:
            raise OSError(errno.ENETUNREACH, "network is unreachable (simulated)")
        self.sent.append((bytes(data), source))
        self.in_flight.append((bytes(data), source))

    def inject(self, data, source=("192.0.2.200", 40000)):
        """Put a datagram on the network as if a foreign host sent it."""
        self.in_flight.append((bytes(data), source))

    def deliver(self, shuffle=False, duplicate=False, max_rounds=100):
        """Deliver in-flight datagrams, including any sent in response,
        until the network is quiet. Returns the number delivered."""
        delivered = 0
        for _ in range(max_rounds):
            if not self.in_flight:
                return delivered
            batch, self.in_flight = self.in_flight, []
            if shuffle:
                self.rng.shuffle(batch)
            for data, source in batch:
                delivered += 1
                for peer in list(self.peers):
                    if self.drop is not None and self.drop(data, source, peer):
                        continue
                    for _ in range(2 if duplicate else 1):
                        peer.handle_datagram(data, source)
        raise AssertionError(f"network still busy after {max_rounds} rounds")
