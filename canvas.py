"""Canvas state for the terminal canvas: a fixed grid of characters and colours.

This module holds state only. It has no terminal or network code, so it can be
tested directly. Events use the same dict schema as protocol.py:

    {"type": "DRAW", "x": 20, "y": 10, "char": "#", "color": 2, ...}
    {"type": "CLEAR", ...}

Extra keys such as "seq" and "addr" are ignored.

Protocol v2 events also carry "lamport" and "peer". Such a *stamped* event
is ordered by its stamp (lamport, peer), compared as a tuple, so every peer
that receives the same events ends with the same canvas whatever order they
arrived in (last-writer-wins):

* each cell remembers the stamp of its last write, and a stamped DRAW is
  applied only if its stamp is larger than both the cell's stamp and the
  canvas-wide clear stamp;
* a stamped CLEAR raises the clear stamp and blanks only the cells written
  before it, so a delayed CLEAR cannot erase later drawing and a delayed
  DRAW older than a CLEAR stays erased.

Events without "lamport" (v1, and calls to draw() and clear()) keep the
original behaviour: they always apply, in arrival order. Cells they write,
and every cell after clear() or restore(), have the stamp NO_STAMP (0, 0),
which any real stamp beats, because peer IDs are never 0.
"""

from __future__ import annotations

import config
import protocol

BLANK = " "
DEFAULT_COLOR = 0
NO_STAMP = (0, 0)
MAX_STAMP_PART = 2**32 - 1


class CanvasError(ValueError):
    """Raised for a malformed event or invalid cell value."""


def _is_int(value) -> bool:
    # bool is a subclass of int but is never a meaningful coordinate.
    return isinstance(value, int) and not isinstance(value, bool)


class Canvas:
    """A width x height grid; (0, 0) is the top-left cell."""

    def __init__(
        self, width: int = config.CANVAS_WIDTH, height: int = config.CANVAS_HEIGHT
    ) -> None:
        # Every cell must be addressable by a protocol coordinate.
        limit = protocol.MAX_COORDINATE + 1
        for name, value in (("width", width), ("height", height)):
            if not _is_int(value) or not 1 <= value <= limit:
                raise CanvasError(f"{name} must be an integer from 1 to {limit}")
        self.width = width
        self.height = height
        self._chars = [[BLANK] * width for _ in range(height)]
        self._colors = [[DEFAULT_COLOR] * width for _ in range(height)]
        self._stamps = [[NO_STAMP] * width for _ in range(height)]
        self.clear_stamp = NO_STAMP

    def in_bounds(self, x, y) -> bool:
        return _is_int(x) and _is_int(y) and 0 <= x < self.width and 0 <= y < self.height

    def cell(self, x: int, y: int) -> tuple[str, int]:
        """Return (char, color) at a cell; raises CanvasError if out of bounds."""
        if not self.in_bounds(x, y):
            raise CanvasError(f"cell ({x}, {y}) is outside {self.width}x{self.height}")
        return self._chars[y][x], self._colors[y][x]

    def cell_stamp(self, x: int, y: int) -> tuple[int, int]:
        """Return the (lamport, peer) stamp of a cell's last write."""
        if not self.in_bounds(x, y):
            raise CanvasError(f"cell ({x}, {y}) is outside {self.width}x{self.height}")
        return self._stamps[y][x]

    def _check_cell(self, x, y, char, color) -> None:
        if not _is_int(x) or not _is_int(y):
            raise CanvasError(f"coordinates must be integers, got ({x!r}, {y!r})")
        if not isinstance(char, str) or len(char) != 1 or not " " <= char <= "~":
            raise CanvasError(f"char must be one printable ASCII character, got {char!r}")
        if not _is_int(color) or not 0 <= color <= protocol.MAX_COLOR:
            raise CanvasError(f"color must be an integer 0-{protocol.MAX_COLOR}, got {color!r}")

    def draw(self, x, y, char, color=DEFAULT_COLOR) -> bool:
        """Set one cell. Returns False, changing nothing, if (x, y) is outside
        the canvas; raises CanvasError for an invalid character or colour."""
        self._check_cell(x, y, char, color)
        if not self.in_bounds(x, y):
            return False
        self._chars[y][x] = char
        self._colors[y][x] = color
        self._stamps[y][x] = NO_STAMP
        return True

    def clear(self) -> None:
        for y in range(self.height):
            self._chars[y] = [BLANK] * self.width
            self._colors[y] = [DEFAULT_COLOR] * self.width
            self._stamps[y] = [NO_STAMP] * self.width
        self.clear_stamp = NO_STAMP

    def _draw_stamped(self, x, y, char, color, stamp) -> bool:
        self._check_cell(x, y, char, color)
        if not self.in_bounds(x, y):
            return False
        if stamp <= self.clear_stamp or stamp <= self._stamps[y][x]:
            return False  # Older than what this cell already shows.
        self._chars[y][x] = char
        self._colors[y][x] = color
        self._stamps[y][x] = stamp
        return True

    def _clear_stamped(self, stamp) -> bool:
        if stamp <= self.clear_stamp:
            return False
        self.clear_stamp = stamp
        for y in range(self.height):
            stamps = self._stamps[y]
            for x in range(self.width):
                if stamps[x] < stamp:
                    self._chars[y][x] = BLANK
                    self._colors[y][x] = DEFAULT_COLOR
                    stamps[x] = NO_STAMP
        return True

    def stamped_cells(self) -> list:
        """Cells a late-join snapshot must carry, row by row, as
        (x, y, char, color, lamport, peer): every cell that is occupied or
        has a stamp (an erased cell's stamp still matters)."""
        return [
            (x, y, self._chars[y][x], self._colors[y][x]) + self._stamps[y][x]
            for y in range(self.height)
            for x in range(self.width)
            if self._stamps[y][x] != NO_STAMP
            or (self._chars[y][x], self._colors[y][x]) != (BLANK, DEFAULT_COLOR)
        ]

    def merge_cell(self, x, y, char, color, lamport, peer) -> bool:
        """Merge one snapshot cell with the last-writer-wins rule.

        An unstamped cell (stamp NO_STAMP, e.g. loaded from a file on the
        sender) fills only a cell that is still blank and unstamped here.
        """
        stamp = (lamport, peer)
        if stamp != NO_STAMP:
            return self._draw_stamped(x, y, char, color, stamp)
        self._check_cell(x, y, char, color)
        if not self.in_bounds(x, y) or self._stamps[y][x] != NO_STAMP:
            return False
        if (self._chars[y][x], self._colors[y][x]) != (BLANK, DEFAULT_COLOR):
            return False
        self._chars[y][x] = char
        self._colors[y][x] = color
        return True

    def merge_clear(self, stamp) -> bool:
        """Merge a snapshot's clear stamp (no effect if NO_STAMP or older)."""
        return tuple(stamp) != NO_STAMP and self._clear_stamped(tuple(stamp))

    @staticmethod
    def _event_stamp(event: dict):
        """(lamport, peer) for a stamped event, None for an unstamped one."""
        if "lamport" not in event:
            return None
        lamport, peer = event["lamport"], event.get("peer")
        for name, value in (("lamport", lamport), ("peer", peer)):
            if not _is_int(value) or not 0 <= value <= MAX_STAMP_PART:
                raise CanvasError(f"{name} must be an integer 0-{MAX_STAMP_PART}, got {value!r}")
        return (lamport, peer)

    def apply_event(self, event) -> bool:
        """Apply a DRAW or CLEAR event; returns True if the canvas changed.

        A DRAW outside the canvas is ignored (returns False), because a peer
        configured with a larger canvas may legitimately send one. A stamped
        event older than the state it would replace is ignored too (see the
        module docstring). Malformed events raise CanvasError.
        """
        if not isinstance(event, dict):
            raise CanvasError(f"event must be a dict, got {type(event).__name__}")
        event_type = event.get("type")
        if event_type == protocol.CLEAR:
            stamp = self._event_stamp(event)
            if stamp is None:
                self.clear()
                return True
            return self._clear_stamped(stamp)
        if event_type == protocol.DRAW:
            stamp = self._event_stamp(event)
            try:
                cell = event["x"], event["y"], event["char"], event["color"]
            except KeyError as exc:
                raise CanvasError(f"DRAW event is missing {exc.args[0]!r}") from None
            if stamp is None:
                return self.draw(*cell)
            return self._draw_stamped(*cell, stamp)
        raise CanvasError(f"unsupported event type {event_type!r}")

    def snapshot(self) -> dict:
        """Return the canvas as plain data: its size and every occupied cell.

        A cell is occupied when its (char, color) differs from a blank cell.
        Cells are listed row by row, left to right.
        """
        cells = [
            {"x": x, "y": y, "char": self._chars[y][x], "color": self._colors[y][x]}
            for y in range(self.height)
            for x in range(self.width)
            if (self._chars[y][x], self._colors[y][x]) != (BLANK, DEFAULT_COLOR)
        ]
        return {"width": self.width, "height": self.height, "cells": cells}

    def restore(self, snapshot) -> None:
        """Replace every cell with the contents of a snapshot() result.

        Cells not listed become blank. The whole snapshot is validated first,
        so on CanvasError the canvas is unchanged. Restored cells are
        unstamped (NO_STAMP), and the clear stamp is reset.
        """
        if not isinstance(snapshot, dict):
            raise CanvasError(f"snapshot must be a dict, got {type(snapshot).__name__}")
        width, height = snapshot.get("width"), snapshot.get("height")
        if (width, height) != (self.width, self.height) or not (_is_int(width) and _is_int(height)):
            raise CanvasError(
                f"canvas size {width!r}x{height!r} does not match {self.width}x{self.height}")
        cells = snapshot.get("cells")
        if not isinstance(cells, list):
            raise CanvasError("cells must be a list")
        if len(cells) > self.width * self.height:
            raise CanvasError(f"{len(cells)} cells do not fit a {self.width}x{self.height} canvas")

        chars = [[BLANK] * self.width for _ in range(self.height)]
        colors = [[DEFAULT_COLOR] * self.width for _ in range(self.height)]
        seen = set()
        for index, cell in enumerate(cells):
            if not isinstance(cell, dict) or set(cell) != {"x", "y", "char", "color"}:
                raise CanvasError(f"cell {index} must have exactly x, y, char and color")
            x, y, char, color = cell["x"], cell["y"], cell["char"], cell["color"]
            if not self.in_bounds(x, y):
                raise CanvasError(f"cell {index} at ({x!r}, {y!r}) is outside the canvas")
            if (x, y) in seen:
                raise CanvasError(f"cell {index} repeats ({x}, {y})")
            seen.add((x, y))
            if not isinstance(char, str) or len(char) != 1 or not " " <= char <= "~":
                raise CanvasError(f"cell {index} char must be one printable ASCII character")
            if not _is_int(color) or not 0 <= color <= protocol.MAX_COLOR:
                raise CanvasError(f"cell {index} color must be an integer 0-{protocol.MAX_COLOR}")
            chars[y][x] = char
            colors[y][x] = color
        self._chars, self._colors = chars, colors
        self._stamps = [[NO_STAMP] * self.width for _ in range(self.height)]
        self.clear_stamp = NO_STAMP

    def row_text(self, y: int) -> str:
        return "".join(self._chars[y])

    def row_colors(self, y: int) -> list[int]:
        return list(self._colors[y])
