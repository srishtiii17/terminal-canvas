# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast
# Implementation and development by Srishti Saluja
"""Protocol v2 coordination on the UI thread.

A Session sits between CanvasApp and a v2 MulticastPeer. It runs on the
main thread only, like the canvas, so it needs no locks of its own:

* it stamps local DRAW and CLEAR events with the next Lamport time and this
  peer's ID before the app applies and sends them;
* it advances the Lamport clock past every received timestamp and applies
  received events with the canvas's last-writer-wins rule;
* it runs late-join synchronization (sync.py): on start() it requests a
  snapshot, buffers live events until the snapshot is merged or the request
  gives up, and answers other peers' requests;
* it runs loss recovery (recovery.py): it sends a HEARTBEAT every second,
  NACKs missing events, and when events cannot be recovered requests a
  snapshot instead (at most once every FALLBACK_INTERVAL seconds).

tick() must be called regularly (the app calls it every loop iteration,
at least every 50 ms); `time_source` and `rng` can be replaced in tests.

v1 mode has no Session; CanvasApp then behaves exactly as before.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable

import protocol
import recovery
import sync
from canvas import CanvasError
from lamport import MAX_TIME, LamportClock

logger = logging.getLogger(__name__)

DATA_TYPES = ("DRAW", "CLEAR")

# Live events held while a snapshot is assembled; beyond this they are
# applied at once, which the last-writer-wins merge also handles correctly.
MAX_BUFFERED = 10000

# Minimum time between snapshot requests caused by unrecoverable events.
FALLBACK_INTERVAL = 5.0


def _is_time(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_TIME


class Session:
    def __init__(
        self,
        canvas,
        peer,
        sync_enabled: bool = True,
        time_source: Callable[[], float] = time.monotonic,
        rng=None,
        recovery_enabled: bool = True,
    ) -> None:
        self.canvas = canvas
        self.peer = peer
        self.clock = LamportClock()
        self.now = time_source
        self.rng = rng if rng is not None else random.SystemRandom()
        self.sync = sync.Synchronizer(canvas, self._send_control, self.rng)
        self.sync_enabled = sync_enabled
        self.buffer: list = []
        self.notices: list = []      # Messages for the app's message line.
        self.send_errors = 0
        self.rejected = 0            # Buffered events that failed on replay.
        # Recovery needs the network's tracker; test doubles may not have one.
        tracker = getattr(peer, "tracker", None)
        self.recovery = (recovery.Recovery(tracker, self._send_control, self.rng, self._lost)
                         if recovery_enabled and tracker is not None else None)
        self.next_heartbeat = 0.0
        self.last_fallback = -FALLBACK_INTERVAL
        self.fallbacks = 0

    @property
    def peer_id(self) -> int:
        return self.peer.peer_id

    # -- local events ----------------------------------------------------

    def stamp(self, event: dict) -> dict:
        """Return a copy of a local event with its Lamport time and peer ID."""
        return {**event, "lamport": self.clock.tick(), "peer": self.peer_id}

    # -- remote messages -------------------------------------------------

    def start(self) -> None:
        """Ask the group for the current canvas (if sync is enabled)."""
        if self.sync_enabled:
            self.sync.request(self.now())
            self.notices.append("Requesting the canvas from other peers...")

    def receive(self, event: dict) -> bool:
        """Handle one remote message; returns True if the canvas changed.

        Raises CanvasError for a malformed DRAW or CLEAR (unless buffered).
        """
        kind = event.get("type")
        if kind in DATA_TYPES:
            if self.sync.waiting and len(self.buffer) < MAX_BUFFERED:
                if _is_time(event.get("lamport")):
                    self.clock.observe(event["lamport"])
                self.buffer.append(event)
                return False
            changed = self.canvas.apply_event(event)  # Validates the event first.
            if "lamport" in event:
                self.clock.observe(event["lamport"])
            return changed
        if kind == "STATE_REQ":
            self.sync.on_request(event, self.now())
            return False
        if kind == "STATE_RESP":
            result = self.sync.on_response(event, self.now())
            if result is None:
                return False
            self._merge(*result)
            return True
        if self.recovery is not None:
            if kind == "NACK":
                self.recovery.on_nack(event, self.now())
            elif kind == "NOT_AVAILABLE":
                self.recovery.on_not_available(event)
        return False  # HEARTBEAT is handled by the network's tracker.

    def _merge(self, clear_stamp, cells, responder) -> None:
        changed = sync.merge_snapshot(self.canvas, clear_stamp, cells)
        self.clock.observe(sync.newest_time(clear_stamp, cells))
        self._replay_buffer()
        self.notices.append(
            f"Canvas synchronised from peer {responder:08x} ({len(cells)} cells, {changed} new)")

    def _replay_buffer(self) -> None:
        buffered, self.buffer = self.buffer, []
        for event in buffered:
            try:
                self.canvas.apply_event(event)
            except CanvasError:
                self.rejected += 1

    # -- timers ----------------------------------------------------------

    def _lost(self, peer: int, seqs: list) -> None:
        """Events from `peer` cannot be recovered: fall back to a snapshot."""
        now = self.now()
        message = f"{len(seqs)} event(s) from peer {peer:08x} could not be recovered"
        if not self.sync.waiting and now - self.last_fallback >= FALLBACK_INTERVAL:
            self.last_fallback = now
            self.fallbacks += 1
            self.sync.request(now)
            message += "; requesting a canvas snapshot"
        self.notices.append(message)

    def tick(self) -> bool:
        """Run timers; returns True if the canvas changed."""
        now = self.now()
        if self.recovery is not None:
            if now >= self.next_heartbeat:
                self.next_heartbeat = now + recovery.HEARTBEAT_INTERVAL
                try:
                    self.peer.send_heartbeat()
                except (OSError, protocol.ProtocolError, RuntimeError) as exc:
                    self.send_errors += 1
                    logger.warning("could not send HEARTBEAT: %s", exc)
            self.recovery.tick(now)
        outcome = self.sync.tick(now)
        if outcome == sync.GAVE_UP:
            had_buffer = bool(self.buffer)
            self._replay_buffer()
            reason = self.sync.last_error or "no peer answered"
            self.notices.append(f"Canvas sync gave up ({reason}); keeping this canvas")
            return had_buffer
        return False

    def _send_control(self, message: dict) -> None:
        try:
            self.peer.send_control({**message, "lamport": self.clock.time})
        except (OSError, protocol.ProtocolError) as exc:
            self.send_errors += 1
            logger.warning("could not send %s: %s", message.get("type"), exc)

    def status(self) -> str:
        """Short status for the app's status line."""
        state = self.sync.state
        if state == sync.WAITING:
            text = f"Sync {self.sync.attempts}/{sync.MAX_ATTEMPTS}"
        else:
            text = {sync.SYNCED: "Synced", sync.GAVE_UP: "Sync none"}.get(state, "")
        if self.recovery is not None:
            stats = self.recovery.stats
            if stats["recovered"] or stats["unrecoverable"]:
                text += f"  Rec {stats['recovered']} Lost {stats['unrecoverable']}"
        auth_failures = getattr(self.peer, "auth_failures", 0)
        if isinstance(auth_failures, int) and auth_failures:
            text += f"  AuthFail {auth_failures}"
        return text.strip()
