# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast
# Implementation and development by Srishti Saluja
"""Receiver-side loss recovery for protocol v2 (NACK scheduling).

The network layer's SequenceTracker already knows, per peer, which
sequence numbers are missing: skipped numbers when a later event arrives,
and numbers announced by a HEARTBEAT that never arrived (tail loss). This
module asks the original sender for them, in the style of SRM and NORM:

1. A newly missing event waits a random 50-150 ms, so several receivers
   that lost the same event do not all ask at once.
2. When the wait expires, contiguous missing numbers from one sender are
   requested with one multicast NACK(target, first, count).
3. A NACK heard from another peer for the same event counts as our own
   request (suppression): we back off instead of repeating it.
4. Each request is retried after 0.2 s, 0.4 s, 0.8 s (exponential back-off),
   at most MAX_NACKS times in all.
5. An event is unrecoverable when the retries are exhausted or the sender
   answers NOT_AVAILABLE (it no longer has it). It is then forgotten by the
   tracker, so it is never requested again, and `on_unrecoverable` is
   called; the Session falls back to a canvas snapshot (late-join sync).

Recovered events arrive as normal DRAW/CLEAR frames with the RETRANSMIT
flag; the tracker delivers each sequence number once and drops further
copies, so an event is never applied twice. Retransmissions are never
NACKed or retransmitted again by anyone else, so there are no loops.

Everything runs on the UI thread and takes `now` as an argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

SEQ_MODULUS = 2**32

NACK_DELAY = (0.05, 0.15)   # Random wait before the first NACK.
RETRY_BACKOFF = 0.2         # Doubles after every attempt.
MAX_NACKS = 3               # NACKs (sent or heard) per missing event.
MAX_RANGE = 256             # Sequence numbers per NACK (network.MAX_NACK_COUNT).
HEARTBEAT_INTERVAL = 1.0    # Seconds between this peer's heartbeats.


@dataclass
class _Entry:
    due: float
    attempts: int = 0


def ranges(seqs) -> list:
    """Group sequence numbers into (first, count) runs, at most MAX_RANGE long.

    A run may wrap from 2**32 - 1 to 0.
    """
    runs = []
    for seq in sorted(seqs):
        if runs:
            first, count = runs[-1]
            if (first + count) % SEQ_MODULUS == seq and count < MAX_RANGE:
                runs[-1] = (first, count + 1)
                continue
        runs.append((seq, 1))
    # Join a run ending at 2**32 - 1 with one starting at 0.
    if len(runs) > 1:
        first, count = runs[-1]
        if (first + count) % SEQ_MODULUS == runs[0][0] and count + runs[0][1] <= MAX_RANGE:
            runs[0] = (first, count + runs[0][1])
            runs.pop()
    return runs


class Recovery:
    """Schedules NACKs for one peer's missing events.

    `tracker` is the network's SequenceTracker (keyed by peer ID),
    `send_control(message)` multicasts a control message, and
    `on_unrecoverable(peer, seqs)` is told about events given up on.
    """

    def __init__(self, tracker, send_control: Callable[[dict], None], rng,
                 on_unrecoverable: Callable[[int, list], None]) -> None:
        self.tracker = tracker
        self._send = send_control
        self.rng = rng
        self._on_unrecoverable = on_unrecoverable
        self.entries: dict = {}  # (peer, seq) -> _Entry
        # "recovered" counts numbers that stopped being missing without being
        # given up: they arrived, or (rarely) fell out of the tracker's
        # 1,024-number window.
        self.stats = {"nacks_sent": 0, "suppressed": 0, "recovered": 0,
                      "unrecoverable": 0, "not_available": 0}

    def tick(self, now: float) -> None:
        missing = {(peer, seq) for peer in self.tracker.senders()
                   for seq in self.tracker.missing(peer)}
        for key in list(self.entries):
            if key not in missing:  # It arrived (possibly retransmitted).
                del self.entries[key]
                self.stats["recovered"] += 1
        for key in missing - self.entries.keys():
            self.entries[key] = _Entry(due=now + self.rng.uniform(*NACK_DELAY))

        due = [key for key, entry in self.entries.items() if entry.due <= now]
        exhausted = [key for key in due if self.entries[key].attempts >= MAX_NACKS]
        self._give_up(exhausted)

        by_peer: dict = {}
        for key in due:
            if key in self.entries:
                by_peer.setdefault(key[0], []).append(key[1])
        for peer, seqs in by_peer.items():
            for first, count in ranges(seqs):
                self._send({"type": "NACK", "target": peer, "first": first, "count": count})
                self.stats["nacks_sent"] += 1
            for seq in seqs:
                self._back_off(self.entries[(peer, seq)], now)

    @staticmethod
    def _back_off(entry: _Entry, now: float) -> None:
        entry.due = now + RETRY_BACKOFF * 2 ** entry.attempts
        entry.attempts += 1

    def on_nack(self, message: dict, now: float) -> None:
        """Another peer asked for events: treat it as our own request."""
        target = message["target"]
        for offset in range(min(message["count"], MAX_RANGE)):
            entry = self.entries.get((target, (message["first"] + offset) % SEQ_MODULUS))
            if entry is not None and entry.due > now:
                self._back_off(entry, now)
                self.stats["suppressed"] += 1

    def on_not_available(self, message: dict) -> None:
        """The sender no longer has these events: give up on them now."""
        peer = message["peer"]
        keys = [(peer, (message["first"] + offset) % SEQ_MODULUS)
                for offset in range(min(message["count"], MAX_RANGE))]
        keys = [key for key in keys if key in self.entries]
        self.stats["not_available"] += len(keys)
        self._give_up(keys)

    def _give_up(self, keys) -> None:
        by_peer: dict = {}
        for key in keys:
            del self.entries[key]
            by_peer.setdefault(key[0], []).append(key[1])
        for peer, seqs in by_peer.items():
            self.tracker.forget_missing(peer, seqs)
            self.stats["unrecoverable"] += len(seqs)
            self._on_unrecoverable(peer, seqs)
