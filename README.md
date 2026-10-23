# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast

*A Serverless Peer-to-Peer Group Communication System using IPv4 Multicast and UDP.*

Peers on the same LAN draw together on a shared terminal ASCII canvas. Every
peer runs the same program, keeps its own copy of the canvas, and is both a
sender and a receiver. Drawing events are sent as compact binary frames over
UDP to an IPv4 multicast group. There is no central application server.

## Status

| Stage | Scope | State |
| --- | --- | --- |
| 1 | Binary protocol (`protocol.py`) and multicast networking (`network.py`) | Done. Tested on one laptop |
| 2 | Enhancement roadmap (`PROJECT_ENHANCEMENT_ROADMAP.docx`) | Done |
| 3 | `canvas.py` (canvas state) and `app.py` (`curses` UI) | Done. Tested on one laptop |
| 4 | Two-laptop test with Wireshark capture | **Not yet performed** (see below) |
| 5 | Per-sender duplicate detection and gap tracking (`tracker.py`), with no protocol change | Done. Tested on one laptop |
| 6 | Final report (`FINAL_PROJECT_REPORT.docx`) | Done. Two-laptop evidence still to be added |
| F | Local canvas save/load (`persistence.py`), local only, with no protocol change | Done. Automated tests |
| Later | Peer ID in the frame, loss recovery, late join, conflict ordering | Proposed only. See the roadmap |

Nothing has been verified between two physical laptops yet.

## Architecture

```text
  Laptop A                                           Laptop B
  app.py      curses UI, keyboard, main loop         app.py
  canvas.py   local canvas replica                   canvas.py
  network.py  ── UDP ─▶  239.255.0.1:5005  ◀─ UDP ── network.py
  tracker.py      (IPv4 multicast, TTL 1, IGMP)      tracker.py
  protocol.py                                        protocol.py
  persistence.py  local JSON save/load (no network)  persistence.py
```

- **Network layer:** IPv4 Class D multicast group `239.255.0.1`. Hosts join
  with `IP_ADD_MEMBERSHIP` and leave with `IP_DROP_MEMBERSHIP`. When a socket
  joins or leaves, the OS sends IGMP messages so that switches and routers know
  which hosts want the group's traffic. IGMP only manages membership. It does
  not carry application data.
- **Transport layer:** UDP datagrams on port `5005`. There are no connections,
  handshakes or retransmissions.
- **Application layer:** one fixed 13-byte binary frame per event, packed with
  `struct`.
- **Concurrency:** a background thread receives frames and puts decoded events
  on a `queue.Queue`. Only the main thread touches the canvas and `curses`.

Each peer uses two sockets:

1. A **receive socket** bound to port 5005 that joins the group.
   `SO_REUSEADDR` (and `SO_REUSEPORT` where available) lets several peers share
   the port on one host.
2. A **send socket** on an ephemeral port that is `connect()`ed to the group,
   with TTL 1 and multicast loopback enabled. Its local `(ip, port)` is unique
   to the peer. When the OS loops a peer's own frame back, the receive path
   recognises that source address and drops the frame. This lets several peers
   run on one machine without echoing their own events.

### Duplicate detection and gap tracking (`tracker.py`)

Every peer numbers its own events from 0, so a sequence number only means
something per sender. A v1 frame has no sender ID, so the receiver treats each
source `(ip, port)` as one sender. Each app session sends from its own
ephemeral port, and Windows hands these out sequentially, so a restarted peer
appears as a new sender. Sequence numbers are compared with RFC 1982
serial-number arithmetic, so wraparound from 2³²−1 to 0 is handled.

| Arriving frame | What happens |
| --- | --- |
| Newer than anything from that sender | Delivered. Any skipped numbers are counted as a gap and remembered as missing (up to 1,024 per sender) |
| One of the remembered missing numbers | Delivered and counted as late, which closes that part of the gap |
| Any other number that is not newer, while the sender is active (silent for less than 2 s): one already received, or a gap older than the 1,024 remembered | Dropped and counted as a duplicate. This also stops a duplicated old `CLEAR` from erasing newer drawing |
| Not newer and not missing (a repeat or an older number), after the sender has been silent for 2 s or more | Treated as a restarted sender (a new session) and delivered. That sender's remembered gaps are forgotten |

At most 256 senders are tracked; the least recently heard is evicted first.
The app's status line shows **Miss**, the skipped numbers that have not
arrived, and **Dup**, the duplicates dropped.

The tracker **detects** loss but does **not recover** it. A missing event
stays missing on that peer's canvas. The packet format is unchanged, so this
needs nothing new from other peers.

## Setup

1. Install Python 3.10 or newer.
2. Install the one runtime dependency. On Windows this is `windows-curses`,
   because Python on Windows has no `curses` module; Linux and macOS need
   nothing extra.

   ```sh
   python -m pip install -r requirements.txt
   ```

   The protocol, network, tracker and canvas modules and all tests use only
   the standard library.
3. Find this laptop's IPv4 address on the network you will use (run
   `ipconfig` and look at the Wi-Fi adapter). The Wi-Fi address changes when
   you reconnect, so check it before every session.
4. Either set `INTERFACE_ADDRESS` in `config.py` to that address or pass it on
   the command line with `--interface`.

**Why the interface matters:** with `INTERFACE_ADDRESS = "0.0.0.0"`, the OS
picks the multicast interface. On machines with virtual adapters (VirtualBox,
Hyper-V, VPNs) it may pick the wrong one. On the development laptop it picked
a VirtualBox host-only adapter (`192.168.56.1`).

## Running the application

```sh
python app.py                          # uses INTERFACE_ADDRESS from config.py
python app.py --interface 192.168.1.23
python app.py --load drawing.json      # start from a saved canvas
python app.py --save drawing.json      # save the canvas when the app exits
```

| Option | Meaning |
| --- | --- |
| `--interface ADDR` | IPv4 address of the network interface to use |
| `--load FILE` | Load `FILE` before joining the group. If it cannot be loaded, the app prints why and exits with status 1 without joining |
| `--save FILE` | Save the canvas to `FILE` when the app exits (`q`, Ctrl+C, or an error). The app exits at startup if `FILE`'s folder does not exist |

Without these options the app behaves as before and creates no file unless
you press `S`. See [Saving and loading](#saving-and-loading-persistencepy).

The terminal must be at least 62 columns by 23 rows. If it is smaller, the app
asks you to enlarge it. The top line shows the group, the interface and the
source address the app sends from. The status line below the canvas shows:

- the brush, colour, pen state and cursor position
- **Sent** and **Recv**: events sent and received
- **Miss** and **Dup**: the gap and duplicate counters described above

| Key | Action |
| --- | --- |
| Arrow keys | Move the cursor |
| Space | Draw the brush character at the cursor |
| `p` | Toggle pen mode (draws while moving) |
| `e` | Erase the cell (draws a space) |
| `b`, then a character | Choose the brush character (Esc cancels) |
| `0`–`7` | Colour: default, red, green, yellow, blue, magenta, cyan, white |
| `X` (Shift+x) | Clear the canvas for all peers |
| `S` (Shift+s) | Save the canvas to a file (local only) |
| `L` (Shift+l) | Replace this canvas with the saved file (local only; peers are not updated) |
| `q` or Ctrl+C | Quit |

What happens when you draw:

- Your drawing is applied to your own canvas immediately, and the event is
  multicast to the other peers.
- The network layer never delivers your own frames back to you, so each
  drawing is applied exactly once.
- Remote events are drawn even while you are not pressing keys, because the
  UI checks for input every 50 ms.
- Warnings and errors go to `terminal_canvas.log` in the current directory,
  because printing them to the console would corrupt the curses screen. The
  file is created only if something is logged.

To try it on one laptop, open two terminals and run `python app.py` in each.

## Saving and loading (`persistence.py`)

Saving and loading are **local only**. Loading replaces this peer's canvas
and sends nothing to the group, so other peers keep their own canvases. To
share a loaded drawing, every peer loads the same file. The UDP protocol,
the 13-byte frame, the group and the port are unchanged.

**Which file the keys use:**

| Options given | `S` saves to | `L` loads from |
| --- | --- | --- |
| none | `canvas.json` in the current folder | `canvas.json` in the current folder |
| `--load A` | `A` | `A` |
| `--save B` | `B` | `B` |
| `--load A --save B` | `B` | `A` |

**Saving is atomic.** The canvas is written to a temporary file in the same
folder, flushed to disk and closed, and then `os.replace()` moves it over
the target. If anything fails, the existing file is left as it was, the
temporary file is deleted, and the message line shows `Save failed: ...`.

**Loading is validated before anything changes.** If the file is rejected,
the canvas stays as it was and the message line shows `Load failed: ...`
(with `--load`, the app prints the reason and exits instead). A file is
rejected if:

- it cannot be read, is not UTF-8, or is larger than 4 MiB (checked before
  parsing)
- it is not valid JSON, or not a terminal canvas save (`"format"` is missing
  or different)
- `"version"` is not `1`, or there are unknown top-level fields
- `"width"`/`"height"` do not match this canvas (60x18 by default)
- `"cells"` is not a list, or lists more cells than the canvas holds
- a cell does not have exactly `x`, `y`, `char` and `color`, is outside the
  canvas, or repeats a position
- a `char` is not one printable ASCII character (`0x20`–`0x7E`), or a `color`
  is not an integer from 0 to 255

### File format (version 1)

UTF-8 JSON:

```json
{
 "format": "terminal-canvas",
 "version": 1,
 "width": 60,
 "height": 18,
 "cells": [
  {"x": 20, "y": 10, "char": "#", "color": 2},
  {"x": 21, "y": 10, "char": "*", "color": 4}
 ]
}
```

- Only occupied cells are stored: cells whose character is not a space or
  whose colour is not 0. All other cells load as blank.
- Cells are listed row by row, left to right.
- Version 1 has no per-cell timestamps, because the canvas has none. A later
  format that adds fields, such as logical timestamps for conflict
  resolution, must use a new version number. Version 1 readers reject unknown
  cell fields instead of silently ignoring them.

## Two-laptop test (Stage 4, still to be done)

1. Put both laptops on the same network. Campus Wi-Fi often isolates clients,
   so prefer a phone hotspot or home router known to allow client-to-client
   traffic, or connect both laptops by Ethernet.
2. On each laptop, run `ipconfig` and note the Wi-Fi IPv4 address.
3. Copy the project folder to both laptops and run
   `python -m pip install -r requirements.txt` on each.
4. Disable VPN clients (such as Cloudflare WARP or McAfee VPN) for the test.
5. On laptop B, start Wireshark on the Wi-Fi interface with the display filter
   `ip.dst == 239.255.0.1 || igmp`.
6. On each laptop, run `python app.py --interface <its own address>`. If
   Windows Firewall asks, allow Python on **Private** networks. If the network
   is marked Public, either switch it to Private or add an inbound rule for UDP
   port 5005.
7. Check each of these:
   - Draw on A and confirm it appears on B.
   - Draw on B and confirm it appears on A.
   - Press `X` on one laptop and confirm both canvases clear.
8. The test passes when all of these hold:
   - Drawing shows up in both directions.
   - Wireshark shows IGMP membership reports for `239.255.0.1`.
   - Wireshark shows UDP datagrams to port 5005 with a UDP length of 21 bytes
     (an 8-byte header plus the 13-byte frame). The data starts with `ca 55`.
9. Save the capture (`.pcapng`) and photos of both screens as evidence.

**Troubleshooting:**

- *Nothing arrives in either direction:* the network probably blocks
  multicast. Try another network.
- *Traffic works in one direction only:* the receiving laptop's firewall is
  probably blocking it.
- *The app fails at startup with "Could not join":* the interface address is
  wrong. Run `ipconfig` again.
- *The app freezes in a classic console window:* clicking inside a classic
  console starts text selection (the window title begins with "Select"), which
  pauses the program. Press Esc. This happened once during the one-laptop
  demonstration. Running the app in Windows Terminal avoids this.

## Packet format

All integers are big-endian (network byte order). The struct format is
`!HBBIhhc`, and the frame size is **13 bytes**.

| Offset | Size | Field | struct | Meaning |
| --- | --- | --- | --- | --- |
| 0 | 2 | Magic | `H` | `0xCA55`, marks frames of this application |
| 2 | 1 | Type | `B` | Message type code (below) |
| 3 | 1 | Color | `B` | Terminal color index, 0–255 |
| 4 | 4 | Sequence | `I` | Per-sender counter, starts at 0, wraps after 2³²−1 |
| 8 | 2 | X | `h` | Canvas column, 0–32767 |
| 10 | 2 | Y | `h` | Canvas row, 0–32767 |
| 12 | 1 | Char | `c` | Printable ASCII character, `0x20`–`0x7E` |

| Code | Type | Status |
| --- | --- | --- |
| 1 | `DRAW` | Implemented |
| 2 | `CLEAR` | Implemented. Color, X and Y are 0 and Char is `0x00` |
| 3 | `STATE_REQ` | Reserved, rejected by encoder and decoder |
| 4 | `STATE_RESP` | Reserved, rejected by encoder and decoder |

Example: draw `#` at (20, 10) in color 7 with sequence `0x01020304`:

```text
CA 55 | 01 | 07 | 01 02 03 04 | 00 14 | 00 0A | 23
magic  DRAW color  sequence      x=20    y=10   '#'
```

### Correction: 12 bytes vs. 13 bytes

The presentation calls this a "12-byte" frame. However, its own fields add up
to 13 bytes (2 + 1 + 1 + 4 + 2 + 2 + 1), and its struct format `!H B B I h h c`
also gives `struct.calcsize(...) == 13`. Every field is required, so the
implementation keeps all of them in exactly that order and format, and the
frame is **13 bytes**. The "12" in the presentation is a counting error.

X and Y use the signed `h` type given in the presentation. Negative values are
rejected, so the usable range is 0–32767.

### Validation

`decode_event()` raises `ProtocolError`, a subclass of `ValueError`, for:

- a size other than 13 bytes
- a wrong magic number
- an unknown or reserved type
- negative coordinates
- a non-printable character
- a `CLEAR` frame with non-zero fields

The receiver counts these datagrams in `MulticastPeer.dropped_count` and drops
them, along with oversized datagrams of any length, so stray traffic on the
group cannot stop the receiver. If an unexpected socket error occurs, the
receiver logs it and the thread exits, and `MulticastPeer.running` becomes
`False`. The app then shows a warning.

The magic number only filters out unrelated traffic. It is **not**
authentication, because anyone on the LAN can send a valid frame.

## Public API

### Event schema

Events are plain dicts:

```python
{"type": "DRAW",  "seq": 7, "x": 20, "y": 10, "char": "#", "color": 2}
{"type": "CLEAR", "seq": 8}
```

`protocol.DRAW` and `protocol.CLEAR` are the type strings. A received event
also has an `"addr"` key holding the sender's `(ip, port)`.

### `protocol.py`

```python
encode_event(event: dict) -> bytes   # 13-byte frame; needs "seq"; ignores extra keys
decode_event(packet: bytes) -> dict  # event dict without "addr"
```

Both functions raise `ProtocolError` on invalid input.

### `network.py`

```python
start_network(on_event, **options) -> MulticastPeer
send_event(event: dict) -> int
stop_network() -> None
```

- **`start_network(on_event)`** joins the group and starts the receive thread.
  - `options` override `config.py`: `group`, `port`, `interface`, `ttl` and
    `loopback`.
  - It raises `RuntimeError` if the network is already started.
  - It raises `OSError` if the sockets cannot be set up, and leaves no sockets
    open when it does.
- **`send_event(event)`** multicasts a `DRAW` or `CLEAR` event and returns the
  sequence number it was given.
  - The network layer owns the sequence counter, so any `"seq"` in the event is
    ignored, and the caller's dict is not modified.
  - It raises `ProtocolError` for an invalid event, without using up a
    sequence number.
  - It raises `RuntimeError` if the network is not started.
- **`stop_network()`** stops the thread, sends `IP_DROP_MEMBERSHIP` and closes
  both sockets. You can call it more than once.

Callback behaviour:

- `on_event(event)` runs on the **receive thread**. `app.py` passes
  `queue.Queue.put` and drains the queue in its main loop.
- The callback is invoked only for valid frames from **other** peers that are
  not duplicates. `MulticastPeer.tracker.totals()` returns the counters
  (`received`, `duplicates`, `gaps`, `late`, `missing`, `restarts`,
  `senders`).
- If the callback raises, the exception is logged and receiving continues.
- After `stop_network()` returns, the callback is not invoked again.

### `canvas.py`

```python
canvas = Canvas(width=60, height=18)  # defaults from config.CANVAS_WIDTH/HEIGHT
canvas.apply_event(event) -> bool     # True if the canvas changed
canvas.draw(x, y, char, color) -> bool
canvas.clear()
canvas.cell(x, y) -> (char, color)
canvas.row_text(y) / canvas.row_colors(y)
```

- **Shared size:** every peer must use the same `CANVAS_WIDTH` and
  `CANVAS_HEIGHT`, because the canvas size is independent of terminal size.
- **Out-of-bounds draws:** a DRAW outside the canvas is ignored and returns
  `False`.
- **Errors:** a malformed event, character or colour raises `CanvasError`, a
  subclass of `ValueError`.
- **Snapshots:** `canvas.snapshot()` returns `{"width", "height", "cells"}`
  with the occupied cells. `canvas.restore(snapshot)` replaces every cell
  with it. It validates the whole snapshot first, so on `CanvasError` the
  canvas is unchanged.

### `persistence.py`

```python
save_canvas(canvas, path)   # atomic write of the version 1 JSON file
load_canvas(canvas, path)   # replace canvas contents with the file
```

Both raise `PersistenceError`, a subclass of `ValueError`, with a readable
reason. A failed load leaves the canvas unchanged, and a failed save leaves
any existing file unchanged.

## Running tests

From the project root:

```sh
python -m compileall -q .
python -m unittest discover -s tests -v
```

The tests need no third-party packages, no terminal and no curses. The suite
has 207 tests: 25 protocol, 52 network, 19 tracker, 19 canvas, 40 app and 52
persistence. The persistence tests (`tests/test_persistence.py`) use
temporary folders and cover saving and loading, rejected files, atomic
writes, the `S`/`L` keys and the `--load`/`--save` options. Run them alone
with `python -m unittest tests.test_persistence -v`.

| Kind | Where | Touches the network? |
| --- | --- | --- |
| Pure unit tests | `test_protocol.py`, `test_canvas.py`, `test_tracker.py`; app logic, run loop and `main()` cleanup in `test_app.py`; `HandleDatagramTests` (including duplicate and gap handling) and `PeerStateTests` in `test_network.py` | No |
| Mocked tests | `MockedSocketTests` and `MockedSpecificInterfaceTests` check socket options, membership join/leave, interface pinning, the receive thread and failure cleanup. `CursesViewTests` checks rendering and key translation with a fake curses module | No |
| Local multicast tests | `LoopbackMulticastTests` (peers) and `TwoAppLoopbackTests` (two `CanvasApp`s). They use real multicast on this laptop, on a random port from 40000 to 49999 | Yes, this host only |

The local multicast tests are **skipped** automatically if the host cannot
send and receive multicast.

**One-laptop evidence** is in `evidence/one_laptop_2026-10-21/`:

- screenshots of two app processes (peers A and B) running side by side
- `demo_capture.pcapng`, a Wireshark/TShark capture of the Wi-Fi interface
  during that run
- `capture_analysis.txt`, which contains:
  - the IGMPv3 join and leave reports
  - 79 datagrams with UDP length 21
  - a hex dump of the first DRAW frame

This evidence shows the laptop transmitting; it does not show reception on
another laptop.

**Not yet performed:** a test between separate physical devices and a
Wireshark capture on the receiving laptop. Each IPv4 packet should be 41 bytes
(55 bytes on the wire including the 14-byte Ethernet header).

## Limitations

- **Saving and loading are local only.** Loading a file does not update
  other peers, so after `L` this canvas can differ from theirs until
  everyone loads the same file. A save made on a canvas of a different size
  cannot be loaded.

- **UDP does not guarantee delivery.** Frames can be lost, duplicated or
  reordered, and there are no acknowledgements or retransmissions.
  - Duplicates are dropped and gaps are counted (Miss).
  - A lost frame is not recovered, so that peer's canvas stays different.
  - A late frame that fills a gap older than the 1,024 most recent missing
    numbers of its sender is dropped as a duplicate.
  - A duplicate that arrives 2 s or more after the sender's last frame would
    be mistaken for a sender restart and applied again. On a LAN, duplicates
    arrive within milliseconds.
- **Concurrent edits can diverge.** Each peer applies its own drawing
  immediately and remote drawing in arrival order. If two users draw on the
  same cell at the same moment, each may end up showing the other's
  character. The proposed fix (Lamport clock plus Peer ID) is in the roadmap.
- **No late-join synchronization.** `STATE_REQ`/`STATE_RESP` are reserved type
  codes only. A peer that joins late sees only events sent after it joined.
- **No authentication or encryption.** Any host on the LAN can read every
  frame or inject valid frames. The magic number is not a security measure.
- **No peer identity in the frame.** A sender is identified only by the source
  `(ip, port)` in `event["addr"]`, which changes when that peer restarts.
  Duplicate and gap tracking therefore works per peer *session*.
- **LAN only.** With TTL 1, frames are not routed beyond the local subnet.
- **The network can block multicast.** Wi-Fi client isolation, VPNs and
  managed switches without IGMP snooping may drop or flood multicast.
