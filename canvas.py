"""Canvas state for the terminal canvas: a fixed grid of characters and colours.

This module holds state only. It has no terminal or network code, so it can be
tested directly. Events use the same dict schema as protocol.py:

    {"type": "DRAW", "x": 20, "y": 10, "char": "#", "color": 2, ...}
    {"type": "CLEAR", ...}

Extra keys such as "seq" and "addr" are ignored.
"""

from __future__ import annotations

import config
import protocol

BLANK = " "
DEFAULT_COLOR = 0


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

    def in_bounds(self, x, y) -> bool:
        return _is_int(x) and _is_int(y) and 0 <= x < self.width and 0 <= y < self.height

    def cell(self, x: int, y: int) -> tuple[str, int]:
        """Return (char, color) at a cell; raises CanvasError if out of bounds."""
        if not self.in_bounds(x, y):
            raise CanvasError(f"cell ({x}, {y}) is outside {self.width}x{self.height}")
        return self._chars[y][x], self._colors[y][x]

    def draw(self, x, y, char, color=DEFAULT_COLOR) -> bool:
        """Set one cell. Returns False, changing nothing, if (x, y) is outside
        the canvas; raises CanvasError for an invalid character or colour."""
        if not _is_int(x) or not _is_int(y):
            raise CanvasError(f"coordinates must be integers, got ({x!r}, {y!r})")
        if not isinstance(char, str) or len(char) != 1 or not " " <= char <= "~":
            raise CanvasError(f"char must be one printable ASCII character, got {char!r}")
        if not _is_int(color) or not 0 <= color <= protocol.MAX_COLOR:
            raise CanvasError(f"color must be an integer 0-{protocol.MAX_COLOR}, got {color!r}")
        if not self.in_bounds(x, y):
            return False
        self._chars[y][x] = char
        self._colors[y][x] = color
        return True

    def clear(self) -> None:
        for y in range(self.height):
            self._chars[y] = [BLANK] * self.width
            self._colors[y] = [DEFAULT_COLOR] * self.width

    def apply_event(self, event) -> bool:
        """Apply a DRAW or CLEAR event; returns True if the canvas changed.

        A DRAW outside the canvas is ignored (returns False), because a peer
        configured with a larger canvas may legitimately send one. Malformed
        events raise CanvasError.
        """
        if not isinstance(event, dict):
            raise CanvasError(f"event must be a dict, got {type(event).__name__}")
        event_type = event.get("type")
        if event_type == protocol.CLEAR:
            self.clear()
            return True
        if event_type == protocol.DRAW:
            try:
                return self.draw(event["x"], event["y"], event["char"], event["color"])
            except KeyError as exc:
                raise CanvasError(f"DRAW event is missing {exc.args[0]!r}") from None
        raise CanvasError(f"unsupported event type {event_type!r}")

    def row_text(self, y: int) -> str:
        return "".join(self._chars[y])

    def row_colors(self, y: int) -> list[int]:
        return list(self._colors[y])
