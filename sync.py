# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast
# Implementation and development by Srishti Saluja
"""Late-join canvas synchronization (protocol v2 STATE_REQ / STATE_RESP).

A peer that joins (or needs to repair its canvas) multicasts STATE_REQ with
a random nonce. Every other peer schedules a reply after a random 0-200 ms
delay, or 200-400 ms if it is itself still waiting for a snapshot (its
canvas may be incomplete, so a synced peer should normally win; but two
peers that start together must still be able to exchange what they have,
and the last-writer-wins merge makes any snapshot safe to merge). The
first to reply sends
the canvas *state* (one record per occupied or stamped cell plus the clear
stamp, never the drawing history) as STATE_RESP chunks, and the others
cancel when they hear a chunk for that nonce.

Chunks carry the snapshot's CRC32, chunk count and canvas size. The joiner
accepts chunks for its own nonce from the first responder only, ignores
exact duplicates, and once every chunk is present checks the CRC, the size
and each record before merging. Live DRAW/CLEAR events received meanwhile
are buffered by the Session and replayed after the merge. The merge uses
the canvas's last-writer-wins rule, so a snapshot cannot overwrite a newer
event, and the result does not depend on arrival order.

If no complete, valid snapshot arrives within REQUEST_TIMEOUT the request
is repeated with a new nonce, up to MAX_ATTEMPTS times; then the joiner
gives up and keeps its current canvas (for example when it is the first
peer, or multicast is blocked).

All state machines take the current time as an argument, so tests drive
them with a fake clock. Everything here runs on the UI thread.
"""

from __future__ import annotations

import struct
import zlib
from typing import Callable, Optional

import protocol_v2
from canvas import Canvas

REQUEST_TIMEOUT = 1.0          # Seconds to wait for a complete snapshot.
PROGRESS_TIMEOUT = 0.5         # Extra time after each new chunk arrives.
MAX_ATTEMPTS = 3               # STATE_REQ sends before giving up.
RESPONSE_DELAY = (0.0, 0.2)    # Random back-off before answering.
WAITING_EXTRA_DELAY = 0.2      # Added while this peer is still waiting itself.
MAX_CHUNKS = 1024              # 81,920 cells; larger snapshots are refused.
REMEMBERED_NONCES = 64         # Requests answered or cancelled, per peer.

CELLS = protocol_v2.MAX_CELLS_PER_CHUNK

# Requester states.
IDLE = "idle"            # Never asked.
WAITING = "waiting"      # STATE_REQ sent, assembling a snapshot.
SYNCED = "synced"        # Snapshot merged.
GAVE_UP = "gave_up"      # No valid snapshot after MAX_ATTEMPTS.


class SnapshotError(ValueError):
    """Raised for an inconsistent, corrupt or unusable snapshot."""


def snapshot_crc(width: int, height: int, clear_stamp, cells) -> int:
    """CRC32 over the canvas size, clear stamp and every cell record."""
    crc = zlib.crc32(struct.pack("!HHII", width, height, *clear_stamp))
    for x, y, char, color, lamport, peer in cells:
        crc = zlib.crc32(struct.pack(protocol_v2.CELL_RECORD_FORMAT, x, y,
                                     char.encode("ascii"), color, lamport, peer), crc)
    return crc


def make_chunks(canvas: Canvas, nonce: int) -> list:
    """Split the canvas state into STATE_RESP message dicts."""
    cells = canvas.stamped_cells()
    groups = [cells[i:i + CELLS] for i in range(0, len(cells), CELLS)] or [[]]
    if len(groups) > MAX_CHUNKS:
        raise SnapshotError(f"snapshot needs {len(groups)} chunks; the limit is {MAX_CHUNKS}")
    crc = snapshot_crc(canvas.width, canvas.height, canvas.clear_stamp, cells)
    return [
        {"type": "STATE_RESP", "nonce": nonce, "index": index, "count": len(groups),
         "crc": crc, "width": canvas.width, "height": canvas.height,
         "clear_stamp": canvas.clear_stamp, "cells": group}
        for index, group in enumerate(groups)
    ]


class Reassembly:
    """Collects the chunks of one snapshot from one responder."""

    def __init__(self, first: dict, width: int, height: int) -> None:
        if (first["width"], first["height"]) != (width, height):
            raise SnapshotError(
                f"snapshot is for a {first['width']}x{first['height']} canvas, "
                f"this one is {width}x{height}")
        if first["count"] > MAX_CHUNKS:
            raise SnapshotError(f"snapshot has {first['count']} chunks; the limit is {MAX_CHUNKS}")
        self.responder = first["peer"]
        self.key = (first["count"], first["crc"], first["width"], first["height"],
                    tuple(first["clear_stamp"]))
        self.chunks: dict = {}
        self.duplicates = 0

    def add(self, chunk: dict) -> bool:
        """Add a chunk; returns True when every chunk is present."""
        key = (chunk["count"], chunk["crc"], chunk["width"], chunk["height"],
               tuple(chunk["clear_stamp"]))
        if key != self.key:
            raise SnapshotError("chunk does not belong to the same snapshot")
        index = chunk["index"]
        if index in self.chunks:
            if self.chunks[index] != chunk["cells"]:
                raise SnapshotError(f"two different copies of chunk {index}")
            self.duplicates += 1
        else:
            self.chunks[index] = chunk["cells"]
        return len(self.chunks) == self.key[0]

    @property
    def missing(self) -> int:
        return self.key[0] - len(self.chunks)

    def result(self):
        """Validate the complete snapshot; returns (clear_stamp, cells)."""
        count, crc, width, height, clear_stamp = self.key
        cells = [cell for index in range(count) for cell in self.chunks[index]]
        if snapshot_crc(width, height, clear_stamp, cells) != crc:
            raise SnapshotError("snapshot CRC32 does not match")
        seen = set()
        for x, y, *_ in cells:
            if not (0 <= x < width and 0 <= y < height):
                raise SnapshotError(f"snapshot cell ({x}, {y}) is outside the canvas")
            if (x, y) in seen:
                raise SnapshotError(f"snapshot repeats cell ({x}, {y})")
            seen.add((x, y))
        return clear_stamp, cells


class Synchronizer:
    """Requester and responder for one peer.

    `send_control(message)` multicasts a control message; `rng` provides
    nonces and back-off delays (random.SystemRandom in the app, a seeded
    random.Random in tests).
    """

    def __init__(self, canvas: Canvas, send_control: Callable[[dict], None], rng) -> None:
        self.canvas = canvas
        self._send = send_control
        self.rng = rng
        self.state = IDLE
        self.nonce: Optional[int] = None
        self.attempts = 0
        self.deadline = 0.0
        self.reassembly: Optional[Reassembly] = None
        self.last_error = ""
        self.pending: dict = {}      # nonce -> time to answer that request.
        self._done_nonces: list = []  # Requests answered or cancelled.
        self.stats = {"requests_sent": 0, "responses_sent": 0, "suppressed": 0,
                      "rejected": 0, "duplicate_chunks": 0, "ignored_chunks": 0}

    @property
    def waiting(self) -> bool:
        return self.state == WAITING

    # -- requester -------------------------------------------------------

    def request(self, now: float) -> None:
        """Start (or restart) a snapshot request with a fresh nonce."""
        if self.state != WAITING:
            self.attempts = 0
        self.state = WAITING
        self._send_request(now)

    def _send_request(self, now: float) -> None:
        self.attempts += 1
        self.nonce = self.rng.getrandbits(32)
        self.reassembly = None
        self.deadline = now + REQUEST_TIMEOUT
        self.stats["requests_sent"] += 1
        self._send({"type": "STATE_REQ", "nonce": self.nonce})

    def on_response(self, chunk: dict, now: float) -> Optional[tuple]:
        """Handle a STATE_RESP chunk.

        Returns (clear_stamp, cells, responder) once a complete, valid
        snapshot for our request has arrived, otherwise None.
        """
        nonce = chunk["nonce"]
        if nonce in self.pending:
            # Another peer is answering this request: do not answer too.
            del self.pending[nonce]
            self._remember(nonce)
            self.stats["suppressed"] += 1
        if self.state != WAITING or nonce != self.nonce:
            return None
        if self.reassembly is None:
            try:
                self.reassembly = Reassembly(chunk, self.canvas.width, self.canvas.height)
            except SnapshotError as exc:
                return self._reject(exc)
        elif chunk["peer"] != self.reassembly.responder:
            self.stats["ignored_chunks"] += 1  # A second responder: keep the first.
            return None
        try:
            before = self.reassembly.duplicates
            complete = self.reassembly.add(chunk)
            self.stats["duplicate_chunks"] += self.reassembly.duplicates - before
            if not complete:
                self.deadline = max(self.deadline, now + PROGRESS_TIMEOUT)
                return None
            clear_stamp, cells = self.reassembly.result()
        except SnapshotError as exc:
            return self._reject(exc)
        responder = self.reassembly.responder
        self.state = SYNCED
        self.reassembly = None
        return clear_stamp, cells, responder

    def _reject(self, exc: SnapshotError) -> None:
        # Drop this snapshot and wait for the timeout, which re-requests.
        self.stats["rejected"] += 1
        self.last_error = str(exc)
        self.reassembly = None
        self.nonce = None
        return None

    # -- responder -------------------------------------------------------

    def on_request(self, request: dict, now: float) -> None:
        nonce = request["nonce"]
        if nonce in self.pending or nonce in self._done_nonces:
            return
        delay = self.rng.uniform(*RESPONSE_DELAY)
        if self.state == WAITING:
            delay += WAITING_EXTRA_DELAY  # Let a synced peer answer first.
        self.pending[nonce] = now + delay

    def _remember(self, nonce: int) -> None:
        self._done_nonces.append(nonce)
        del self._done_nonces[:-REMEMBERED_NONCES]

    # -- timers ----------------------------------------------------------

    def tick(self, now: float) -> Optional[str]:
        """Send due responses and handle request timeouts.

        Returns "gave_up" when a request has finally failed, else None.
        """
        for nonce, due in list(self.pending.items()):
            if now >= due:
                del self.pending[nonce]
                self._remember(nonce)
                for chunk in make_chunks(self.canvas, nonce):
                    self._send(chunk)
                self.stats["responses_sent"] += 1
        if self.state == WAITING and now >= self.deadline:
            if self.reassembly is not None:
                self.last_error = f"snapshot incomplete: {self.reassembly.missing} chunk(s) missing"
            if self.attempts < MAX_ATTEMPTS:
                self._send_request(now)
            else:
                self.state = GAVE_UP
                self.nonce = None
                self.reassembly = None
                return GAVE_UP
        return None


def merge_snapshot(canvas: Canvas, clear_stamp, cells) -> int:
    """Merge a validated snapshot into `canvas`; returns cells changed."""
    canvas.merge_clear(clear_stamp)
    return sum(canvas.merge_cell(*cell) for cell in cells)


def newest_time(clear_stamp, cells) -> int:
    """Largest Lamport time in a snapshot (for advancing the clock)."""
    return max([clear_stamp[0]] + [cell[4] for cell in cells])

