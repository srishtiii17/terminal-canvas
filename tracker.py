"""Per-sender duplicate detection and sequence-gap tracking for v1 frames.

A v1 frame carries a per-sender sequence number but no sender identifier, so
a sender is identified by the source (ip, port) of its datagrams. Each peer
sends from its own connected socket on an ephemeral port, so this address
identifies one peer session; a restarted peer gets a new port.

Sequence numbers are compared with serial-number arithmetic (RFC 1982 style)
so the tracker keeps working when a sender's counter wraps from 2**32 - 1
to 0. For each frame, observe() returns a Verdict:

    NEW        first frame from a sender, or newer than any seen before
    LATE       fills an earlier gap (it arrived out of order)
    DUPLICATE  this sequence number was already received; drop the frame

The tracker only detects loss; it does not recover missing events.
"""

from __future__ import annotations

import enum
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Hashable

SEQ_MODULUS = 2**32
HALF_RANGE = 2**31

# Missing sequence numbers remembered per sender, so a late arrival can still
# be recognised. Older gaps stay counted but are forgotten.
MAX_MISSING = 1024

# Senders tracked at once; the least recently heard sender is evicted first.
MAX_SENDERS = 256

# An older, non-missing sequence number is a duplicate only while the sender
# is active. After this much silence it means the sender restarted and
# happened to reuse the same port, so a new session starts instead.
IDLE_RESET_SECONDS = 2.0


class Verdict(enum.Enum):
    NEW = "new"
    LATE = "late"
    DUPLICATE = "duplicate"


@dataclass
class SenderStats:
    received: int = 0     # Frames accepted (NEW or LATE).
    duplicates: int = 0   # Frames dropped as duplicates.
    gaps: int = 0         # Sequence numbers skipped when newer frames arrived.
    late: int = 0         # Skipped numbers that arrived afterwards.
    restarts: int = 0     # Times the sender appeared to start a new session.

    @property
    def missing(self) -> int:
        """Skipped sequence numbers that have not (yet) arrived."""
        return self.gaps - self.late


class _Stream:
    def __init__(self, seq: int, now: float) -> None:
        self.highest = seq
        self.last_seen = now
        self.missing: "OrderedDict[int, None]" = OrderedDict()
        self.stats = SenderStats(received=1)


class SequenceTracker:
    """Thread-safe: observe() runs on the receive thread, totals() on the UI."""

    def __init__(
        self,
        max_senders: int = MAX_SENDERS,
        max_missing: int = MAX_MISSING,
        idle_reset_seconds: float = IDLE_RESET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_senders = max_senders
        self.max_missing = max_missing
        self.idle_reset_seconds = idle_reset_seconds
        self._clock = clock
        self._streams: "OrderedDict[Hashable, _Stream]" = OrderedDict()
        self._evicted = SenderStats()  # Counts kept from evicted senders.
        self._lock = threading.Lock()

    def observe(self, sender: Hashable, seq: int) -> Verdict:
        if not 0 <= seq < SEQ_MODULUS:
            raise ValueError(f"sequence number out of range: {seq}")
        with self._lock:
            now = self._clock()
            stream = self._streams.get(sender)
            if stream is None:
                self._add_stream(sender, _Stream(seq, now))
                return Verdict.NEW
            self._streams.move_to_end(sender)
            verdict = self._classify(stream, seq, now)
            stream.last_seen = now
            return verdict

    def _classify(self, stream: _Stream, seq: int, now: float) -> Verdict:
        stats = stream.stats
        distance = (seq - stream.highest) % SEQ_MODULUS

        if 0 < distance < HALF_RANGE:  # Newer than anything seen.
            skipped = distance - 1
            if skipped:
                stats.gaps += skipped
                # Remember only the most recent max_missing skipped numbers.
                for back in range(min(skipped, self.max_missing), 0, -1):
                    stream.missing[(seq - back) % SEQ_MODULUS] = None
                while len(stream.missing) > self.max_missing:
                    stream.missing.popitem(last=False)
            stream.highest = seq
            stats.received += 1
            return Verdict.NEW

        if seq in stream.missing:  # Out of order: fills a gap.
            del stream.missing[seq]
            stats.late += 1
            stats.received += 1
            return Verdict.LATE

        if now - stream.last_seen >= self.idle_reset_seconds:
            # A silent sender repeating or "going backwards" restarted on the
            # same port; real network duplicates arrive within milliseconds.
            stream.highest = seq
            stream.missing.clear()
            stats.restarts += 1
            stats.received += 1
            return Verdict.NEW

        stats.duplicates += 1
        return Verdict.DUPLICATE

    def observe_heartbeat(self, sender: Hashable, next_seq: int) -> int:
        """Record that `sender` has sent every number below `next_seq`.

        Used by protocol v2 HEARTBEAT messages to expose tail loss (the
        last events of a burst lost while the sender then stays quiet).
        Numbers not yet received become missing, as for a gap; returns how
        many became missing. A sender never heard before gets its baseline
        here, without reporting earlier events as missing.
        """
        if not 0 <= next_seq < SEQ_MODULUS:
            raise ValueError(f"sequence number out of range: {next_seq}")
        latest = (next_seq - 1) % SEQ_MODULUS
        with self._lock:
            now = self._clock()
            stream = self._streams.get(sender)
            if stream is None:
                stream = _Stream(latest, now)
                stream.stats.received = 0
                self._add_stream(sender, stream)
                return 0
            self._streams.move_to_end(sender)
            stream.last_seen = now
            distance = (latest - stream.highest) % SEQ_MODULUS
            if not 0 < distance < HALF_RANGE:
                return 0
            for back in range(min(distance, self.max_missing) - 1, -1, -1):
                stream.missing[(latest - back) % SEQ_MODULUS] = None
            while len(stream.missing) > self.max_missing:
                stream.missing.popitem(last=False)
            stream.stats.gaps += distance
            stream.highest = latest
            return distance

    def senders(self) -> list:
        """The senders currently tracked."""
        with self._lock:
            return list(self._streams)

    def missing(self, sender: Hashable) -> list:
        """Remembered missing sequence numbers of one sender, oldest first."""
        with self._lock:
            stream = self._streams.get(sender)
            return list(stream.missing) if stream else []

    def forget_missing(self, sender: Hashable, seqs) -> None:
        """Stop remembering numbers that will not be recovered. They stay
        counted as missing; if one still arrives it is dropped as old."""
        with self._lock:
            stream = self._streams.get(sender)
            if stream is not None:
                for seq in seqs:
                    stream.missing.pop(seq, None)

    def _add_stream(self, sender: Hashable, stream: _Stream) -> None:
        self._streams[sender] = stream
        while len(self._streams) > self.max_senders:
            _, evicted = self._streams.popitem(last=False)
            for field in ("received", "duplicates", "gaps", "late", "restarts"):
                setattr(self._evicted, field,
                        getattr(self._evicted, field) + getattr(evicted.stats, field))

    def stats(self, sender: Hashable) -> SenderStats:
        """A copy of one sender's counters (all zero if never seen)."""
        with self._lock:
            stream = self._streams.get(sender)
            return SenderStats(**vars(stream.stats)) if stream else SenderStats()

    def totals(self) -> dict:
        """Counters summed over all senders, including evicted ones."""
        with self._lock:
            total = SenderStats(**vars(self._evicted))
            for stream in self._streams.values():
                for field in ("received", "duplicates", "gaps", "late", "restarts"):
                    setattr(total, field, getattr(total, field) + getattr(stream.stats, field))
            return {
                "senders": len(self._streams),
                "received": total.received,
                "duplicates": total.duplicates,
                "gaps": total.gaps,
                "late": total.late,
                "missing": total.missing,
                "restarts": total.restarts,
            }

    def reset(self) -> None:
        with self._lock:
            self._streams.clear()
            self._evicted = SenderStats()
