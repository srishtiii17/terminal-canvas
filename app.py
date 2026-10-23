"""Terminal UI for the collaborative canvas.

Run on each laptop:

    python app.py [--interface 192.168.1.23]

Responsibilities are split so the logic is testable without a terminal:

* CanvasApp - application state and key handling. It applies local drawing to
  the canvas once and sends it; it drains remote events from a queue.
* CursesView - the only code that touches curses: rendering and key reading.
* main()     - starts the network, runs the curses loop, always stops the
  network and restores the terminal.

Threading: the network receive thread only calls inbox.put(). The canvas and
curses are used from the main thread alone.

On Windows curses comes from the windows-curses package (requirements.txt).
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
from typing import Callable, Optional

import config
import network
import protocol
from canvas import BLANK, DEFAULT_COLOR, Canvas, CanvasError

# How long getch() waits for a key, so remote events are drawn promptly even
# when the user is idle.
INPUT_TIMEOUT_MS = 50

# Remote events applied per loop iteration, so a burst cannot freeze input.
MAX_REMOTE_EVENTS_PER_FRAME = 500

COLOR_NAMES = ["default", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]

HELP_TEXT = "Arrows move  Space draw  p pen  e erase  b brush  0-7 colour  X clear all  q quit"

LOG_FILE = "terminal_canvas.log"


class CanvasApp:
    """Application state and behaviour, independent of curses.

    Keys arrive as strings: "UP", "DOWN", "LEFT", "RIGHT", "ESC", "CTRL_C",
    "RESIZE", or a single printable character.
    """

    def __init__(
        self,
        canvas: Canvas,
        send: Callable[[dict], int],
        inbox: "queue.Queue[dict]",
        network_ok: Callable[[], bool] = lambda: True,
        network_label: str = "",
        network_stats: Callable[[], dict] = dict,
    ) -> None:
        self.canvas = canvas
        self.inbox = inbox
        self.network_label = network_label
        # Returns SequenceTracker.totals(): "missing" and "duplicates" counts.
        self.network_stats = network_stats
        self._send = send
        self._network_ok = network_ok
        self._network_warned = False

        self.cursor_x = 0
        self.cursor_y = 0
        self.brush = "#"
        self.color = DEFAULT_COLOR
        self.pen_down = False
        self.awaiting_brush = False
        self.running = True
        self.dirty = True  # The view must redraw.
        self.message = ""

        self.sent_count = 0
        self.received_count = 0
        self.rejected_count = 0
        self.send_errors = 0

    # -- keyboard ----------------------------------------------------------

    def handle_key(self, key: str) -> None:
        self.dirty = True
        self.message = ""

        if key == "CTRL_C":
            self.running = False
            return
        if key == "RESIZE":
            return
        if self.awaiting_brush:
            self._choose_brush(key)
            return

        moves = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}
        if key in moves:
            self.move(*moves[key])
        elif key == " ":
            self.stamp(self.brush, self.color)
        elif key == "e":
            self.stamp(BLANK, DEFAULT_COLOR)
        elif key == "p":
            self.pen_down = not self.pen_down
            if self.pen_down:
                self.stamp(self.brush, self.color)
        elif key == "b":
            self.awaiting_brush = True
            self.message = "Press the character to draw with (Esc cancels)"
        elif len(key) == 1 and "0" <= key <= "7":
            self.color = int(key)
        elif key == "X":
            self.clear_all()
        elif key in ("q", "Q"):
            self.running = False
        else:
            self.message = f"Unknown key {key!r}. {HELP_TEXT}"

    def _choose_brush(self, key: str) -> None:
        if key == "ESC":
            self.awaiting_brush = False
        elif len(key) == 1 and " " < key <= "~":
            self.brush = key
            self.awaiting_brush = False
        else:
            self.message = "Brush must be a visible ASCII character (Esc cancels)"

    # -- actions -----------------------------------------------------------

    def move(self, dx: int, dy: int) -> None:
        self.cursor_x = min(max(self.cursor_x + dx, 0), self.canvas.width - 1)
        self.cursor_y = min(max(self.cursor_y + dy, 0), self.canvas.height - 1)
        if self.pen_down:
            self.stamp(self.brush, self.color)

    def stamp(self, char: str, color: int) -> None:
        """Draw at the cursor: apply locally once, then send to peers.

        The network layer never delivers our own frames back, so the event
        is applied exactly once.
        """
        event = {
            "type": protocol.DRAW,
            "x": self.cursor_x,
            "y": self.cursor_y,
            "char": char,
            "color": color,
        }
        self.canvas.apply_event(event)
        self._send_event(event)

    def clear_all(self) -> None:
        event = {"type": protocol.CLEAR}
        self.canvas.apply_event(event)
        self._send_event(event)
        self.message = "Canvas cleared for all peers"

    def _send_event(self, event: dict) -> None:
        try:
            self._send(event)
        except (OSError, protocol.ProtocolError) as exc:
            # Keep running: the local canvas is still usable.
            self.send_errors += 1
            self.message = f"Send failed ({exc}); peers did not get this change"
        else:
            self.sent_count += 1

    # -- network -----------------------------------------------------------

    def process_remote_events(self, limit: int = MAX_REMOTE_EVENTS_PER_FRAME) -> int:
        """Apply up to `limit` queued remote events; returns how many were taken."""
        taken = 0
        while taken < limit:
            try:
                event = self.inbox.get_nowait()
            except queue.Empty:
                break
            taken += 1
            try:
                self.canvas.apply_event(event)
            except CanvasError:
                self.rejected_count += 1
            else:
                self.received_count += 1
            self.dirty = True

        if not self._network_warned and not self._network_ok():
            self._network_warned = True
            self.message = f"Network receiver stopped; see {LOG_FILE}. Restart the app."
            self.dirty = True
        return taken


class CursesView:
    """Renders a CanvasApp with curses and translates key codes."""

    def __init__(self, stdscr, curses_module) -> None:
        self.stdscr = stdscr
        self.curses = curses_module
        stdscr.keypad(True)
        stdscr.timeout(INPUT_TIMEOUT_MS)
        try:
            curses_module.curs_set(1)
        except curses_module.error:
            pass  # Cursor visibility is cosmetic.
        self._color_attrs = self._init_colors()

    def _init_colors(self) -> dict:
        c = self.curses
        if not c.has_colors():
            return {}
        try:
            c.start_color()
            background = -1
            try:
                c.use_default_colors()
            except c.error:
                background = c.COLOR_BLACK
            foregrounds = [c.COLOR_RED, c.COLOR_GREEN, c.COLOR_YELLOW, c.COLOR_BLUE,
                           c.COLOR_MAGENTA, c.COLOR_CYAN, c.COLOR_WHITE]
            attrs = {}
            for index, foreground in enumerate(foregrounds, start=1):
                c.init_pair(index, foreground, background)
                attrs[index] = c.color_pair(index)
            return attrs
        except c.error:
            return {}

    def read_key(self) -> Optional[str]:
        """Wait up to INPUT_TIMEOUT_MS for a key; None if there was none."""
        c = self.curses
        code = self.stdscr.getch()
        if code == -1:
            return None
        named = {c.KEY_UP: "UP", c.KEY_DOWN: "DOWN", c.KEY_LEFT: "LEFT",
                 c.KEY_RIGHT: "RIGHT", 27: "ESC", 3: "CTRL_C"}
        if code in named:
            return named[code]
        if code == c.KEY_RESIZE:
            try:
                c.resize_term(0, 0)  # Needed by PDCurses on Windows.
            except (AttributeError, c.error):
                pass
            return "RESIZE"
        if 32 <= code <= 126:
            return chr(code)
        return None

    def render(self, app: CanvasApp) -> None:
        scr = self.stdscr
        canvas = app.canvas
        rows, cols = scr.getmaxyx()
        need_cols, need_rows = canvas.width + 2, canvas.height + 5
        scr.erase()

        if rows < need_rows or cols < need_cols:
            self._put(0, 0, f"Terminal too small: need {need_cols}x{need_rows}, "
                            f"have {cols}x{rows}.", cols)
            self._put(1, 0, "Enlarge the window, or press q to quit.", cols)
            scr.refresh()
            return

        self._put(0, 0, f"Terminal Canvas  {app.network_label}", cols)
        border = "+" + "-" * canvas.width + "+"
        self._put(1, 0, border, cols)
        for y in range(canvas.height):
            screen_y = 2 + y
            self._put(screen_y, 0, "|", cols)
            self._render_row(screen_y, canvas.row_text(y), canvas.row_colors(y), cols)
            self._put(screen_y, canvas.width + 1, "|", cols)
        self._put(canvas.height + 2, 0, border, cols)

        pen = "ON " if app.pen_down else "off"
        status = (f"Brush '{app.brush}'  Colour {app.color} {COLOR_NAMES[app.color]:<7}  "
                  f"Pen {pen}  ({app.cursor_x},{app.cursor_y})  "
                  f"Sent {app.sent_count}  Recv {app.received_count}")
        stats = app.network_stats()
        if stats:
            status += f"  Miss {stats['missing']}  Dup {stats['duplicates']}"
        if app.rejected_count or app.send_errors:
            status += f"  Rejected {app.rejected_count}  SendErr {app.send_errors}"
        self._put(canvas.height + 3, 0, status, cols)
        self._put(canvas.height + 4, 0, app.message or HELP_TEXT, cols)

        try:
            scr.move(2 + app.cursor_y, 1 + app.cursor_x)
        except self.curses.error:
            pass
        scr.refresh()

    def _render_row(self, screen_y: int, text: str, colors: list, cols: int) -> None:
        # Write runs of equal colour in one call each.
        start = 0
        while start < len(text):
            end = start
            while end < len(text) and colors[end] == colors[start]:
                end += 1
            self._put(screen_y, 1 + start, text[start:end], cols,
                      self._color_attrs.get(colors[start], 0))
            start = end

    def _put(self, y: int, x: int, text: str, cols: int, attr: int = 0) -> None:
        # Never write the last column: curses raises when the cursor would
        # move past the bottom-right corner.
        width = cols - 1 - x
        if width <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, text, width, attr)
        except self.curses.error:
            pass


def run_loop(app: CanvasApp, view) -> None:
    """Main loop: apply remote events, redraw when needed, handle one key."""
    while app.running:
        app.process_remote_events()
        if app.dirty:
            view.render(app)
            app.dirty = False
        key = view.read_key()  # Waits at most INPUT_TIMEOUT_MS.
        if key is not None:
            app.handle_key(key)


def load_curses():
    """Return the curses module, or None if it is not installed."""
    try:
        import curses
    except ImportError:
        return None
    return curses


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serverless collaborative ASCII canvas over UDP multicast.")
    parser.add_argument(
        "--interface", default=config.INTERFACE_ADDRESS,
        help="IPv4 address of the network interface to use, e.g. this laptop's "
             f"Wi-Fi address from ipconfig (default: {config.INTERFACE_ADDRESS})")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    curses = load_curses()
    if curses is None:
        print("curses is not available. On Windows install it with:\n"
              "    python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    # Log to a file: messages written to the console would corrupt the
    # curses screen. delay=True creates the file only if something is logged.
    handler = logging.FileHandler(LOG_FILE, delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[handler])

    group = f"{config.MULTICAST_GROUP}:{config.MULTICAST_PORT}"
    inbox: "queue.Queue[dict]" = queue.Queue()
    try:
        peer = network.start_network(inbox.put, interface=args.interface)
    except OSError as exc:
        print(f"Could not join {group} on interface {args.interface}: {exc}\n"
              "Check that this address is this computer's current IPv4 address "
              "(run ipconfig), or pass --interface with the correct one.",
              file=sys.stderr)
        return 1

    app = CanvasApp(
        Canvas(),
        network.send_event,
        inbox,
        network_ok=lambda: peer.running,
        network_stats=peer.tracker.totals,
        network_label=f"group {group}  via {args.interface}  "
                      f"(sending from {peer.local_address[0]}:{peer.local_address[1]})",
    )
    try:
        curses.wrapper(lambda stdscr: run_loop(app, CursesView(stdscr, curses)))
    except KeyboardInterrupt:
        pass  # Ctrl+C: curses.wrapper has already restored the terminal.
    finally:
        network.stop_network()
    print(f"Stopped. Sent {app.sent_count} events, received {app.received_count}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
