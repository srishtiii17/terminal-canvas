# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast
# Implementation and development by Srishti Saluja
"""Local save and load of the canvas as a JSON file.

Persistence is local only: loading a file replaces this peer's canvas and
sends nothing to other peers. The network protocol is not involved.

File format (version 1), UTF-8 JSON::

    {
      "format": "terminal-canvas",
      "version": 1,
      "width": 60,
      "height": 18,
      "cells": [
        {"x": 20, "y": 10, "char": "#", "color": 2}
      ]
    }

Only occupied cells are stored, that is cells whose (char, color) differs
from a blank cell. A later format that adds per-cell fields (for example
logical timestamps) must use a new version number; version 1 readers reject
unknown cell fields instead of silently dropping them.

Saving is atomic: the JSON is written to a temporary file in the target's
directory, flushed to disk and closed, then moved over the target with
os.replace(). A failed save leaves any existing file unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile

from canvas import Canvas, CanvasError

FORMAT_NAME = "terminal-canvas"
FORMAT_VERSION = 1

# Far above any real save: a full 60x18 canvas is about 50 KB. Larger files
# are rejected before parsing.
MAX_FILE_BYTES = 4 * 1024 * 1024

DEFAULT_FILE = "canvas.json"


class PersistenceError(ValueError):
    """Raised when a canvas cannot be saved to or loaded from a file."""


def dumps(canvas: Canvas) -> str:
    """Serialize a canvas to the version 1 JSON text."""
    snapshot = canvas.snapshot()
    document = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "width": snapshot["width"],
        "height": snapshot["height"],
        "cells": snapshot["cells"],
    }
    return json.dumps(document, indent=1) + "\n"


def loads(text: str, canvas: Canvas) -> None:
    """Parse version 1 JSON text into `canvas`, replacing its contents.

    Raises PersistenceError, leaving `canvas` unchanged, if the text is not
    a valid save for a canvas of this size.
    """
    try:
        document = json.loads(text)
    except ValueError as exc:  # JSONDecodeError, or invalid constants
        raise PersistenceError(f"not valid JSON ({exc})") from None
    if not isinstance(document, dict):
        raise PersistenceError("not a terminal canvas save (top level is not an object)")
    if document.get("format") != FORMAT_NAME:
        raise PersistenceError("not a terminal canvas save (missing format marker)")
    version = document.get("version")
    if version != FORMAT_VERSION or isinstance(version, bool):
        raise PersistenceError(
            f"unsupported file version {version!r}; this program reads version {FORMAT_VERSION}")
    unknown = set(document) - {"format", "version", "width", "height", "cells"}
    if unknown:
        raise PersistenceError(f"unexpected fields {sorted(unknown)}")
    snapshot = {key: document.get(key) for key in ("width", "height", "cells")}
    try:
        canvas.restore(snapshot)
    except CanvasError as exc:
        raise PersistenceError(str(exc)) from None


def save_canvas(canvas: Canvas, path: str) -> None:
    """Write `canvas` to `path` atomically. Raises PersistenceError on failure."""
    text = dumps(canvas)
    directory = os.path.dirname(os.path.abspath(path))
    try:
        fd, temp_path = tempfile.mkstemp(
            dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    except OSError as exc:
        raise PersistenceError(f"cannot write {path}: {exc.strerror or exc}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise PersistenceError(f"cannot write {path}: {exc.strerror or exc}") from None


def load_canvas(canvas: Canvas, path: str) -> None:
    """Replace `canvas` with the save in `path`.

    Raises PersistenceError, leaving `canvas` unchanged, if the file cannot
    be read or is not a valid save for a canvas of this size.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise PersistenceError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(data) > MAX_FILE_BYTES:
        raise PersistenceError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PersistenceError(f"{path} is not UTF-8 text") from None
    try:
        loads(text, canvas)
    except PersistenceError as exc:
        raise PersistenceError(f"{path}: {exc}") from None
