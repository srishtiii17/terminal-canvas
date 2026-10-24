# Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast

*A Serverless Peer-to-Peer Group Communication System using IPv4 Multicast and UDP.*

Peers on the same LAN draw together on a shared terminal ASCII canvas. Every
peer runs the same program, keeps its own copy of the canvas, and is both a
sender and a receiver. Drawing events are sent as compact binary frames over
UDP to an IPv4 multicast group. There is no central application server.

## Authors

Computer Networks project team, as listed in `FINAL_PROJECT_REPORT.docx`
and `PROJECT_ENHANCEMENT_ROADMAP.docx`:

- Srishti Saluja (24BCE2587)
- Vidushi Kapoor (24BCE2244)

**Implementation and development:** Srishti Saluja

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
| A | Protocol v2 (`protocol_v2.py`): versioned 20-byte header with a random peer ID; `--protocol 2` | Done. Automated and one-laptop loopback tests |
| D | Lamport timestamps and deterministic last-writer-wins (`lamport.py`, `session.py`, `canvas.py`), v2 only | Done. Automated tests |
| C | Late-join canvas synchronization with STATE_REQ/STATE_RESP (`sync.py`), v2 only | Done. Automated and one-laptop loopback tests |
| B | Packet-loss recovery: heartbeat, NACK, retransmission, NOT_AVAILABLE, snapshot fallback (`recovery.py`), v2 only | Done. Automated tests with simulated loss |
| E | Optional HMAC-SHA256 authentication with a shared passphrase (`auth.py`, `--auth`), v2 only | Done. Automated tests |

Nothing in this repository records a test between two physical laptops.
**Phases F, A, D, C, B and E were validated on one laptop only:**

- unit tests;
- an in-memory multi-peer network (`tests/simnet.py`) with fake clocks and
  deterministic packet loss;
- real multicast loopback between peers on the same host.

**Physical Windows–Mac validation of protocol v2 (peer identity, conflict
resolution, late join, loss recovery and authentication) remains pending.**

## Architecture

```text
  Laptop A                                           Laptop B
  app.py      curses UI, keyboard, main loop         app.py
  canvas.py   local canvas replica                   canvas.py
  network.py  ── UDP ─▶  239.255.0.1:5005  ◀─ UDP ── network.py
  tracker.py      (IPv4 multicast, TTL 1, IGMP)      tracker.py
  protocol.py                                        protocol.py
  persistence.py  local JSON save/load (no network)  persistence.py

  v2 only (--protocol 2):
  protocol_v2.py  20-byte versioned header, all message types
  session.py      UI-thread coordinator: Lamport stamps, sync, recovery timers
  lamport.py      Lamport clock          sync.py      late-join snapshots
  recovery.py     NACK scheduling        auth.py      optional HMAC-SHA256
```

In v2 the receive thread still only decodes, filters duplicates (by peer
ID) and answers NACKs for its own events from its send history. Everything
that touches the canvas, including sync and recovery timers, runs on the
UI thread in `session.Session`, driven by `CanvasApp.tick()` at least every
50 ms.

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
python app.py --protocol 2             # use protocol v2 (every peer must)
python app.py --protocol 2 --simulate-loss 20   # drop 20% of received datagrams
```

| Option | Meaning |
| --- | --- |
| `--interface ADDR` | IPv4 address of the network interface to use |
| `--protocol {1,2}` | Wire protocol. `1` (default) is the original 13-byte frame. `2` is the versioned format described in [Protocol v2](#protocol-v2-protocol_v2py). Every peer in a session must use the same protocol |
| `--simulate-loss PERCENT` | Drop this percentage (0–90) of received datagrams on purpose, to demonstrate loss recovery on one laptop. The top line then shows `SIMULATED LOSS` |
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

**Single-laptop v2 demonstration.** Run each command in its own terminal,
and use the same options in every terminal:

1. Start the first peer: `python app.py --protocol 2`. Draw something. The
   status line shows `Sync none`, because no other peer answered its
   request.
2. Start a second peer the same way. It shows `Synced`, and the drawing
   from step 1 appears: this is the late join.
3. Draw the same cell in both terminals at once. Both end with the same
   character: this is the Lamport conflict resolution.
4. Restart one terminal with `--simulate-loss 30`. Its status line shows
   `Rec n Lost m` while recovery repairs the losses.
5. For authentication, set `TERMINAL_CANVAS_PASSPHRASE` in every terminal
   and add `--auth`. A terminal with a different passphrase shows
   `AuthFail n` and never receives anything.

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

## Packet format (protocol v1, the default)

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

## Protocol v2 (`protocol_v2.py`)

Protocol v2 is opt-in (`--protocol 2`). v1 stays the default, and
`protocol.py` and its tests are unchanged. The two versions do not
interoperate:

- A v1 peer drops every v2 datagram, because none is 13 bytes long, and
  counts it in `dropped_count`.
- A v2 peer drops every v1 frame, counts it in `MulticastPeer.legacy_count`,
  and logs one warning per sender address.

### Header (20 bytes)

Big-endian, struct format `!HBBBBIIIH`:

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 2 | Magic | `0xCA55`, as in v1 |
| 2 | 1 | Version | `2` |
| 3 | 1 | Type | Message type (below) |
| 4 | 1 | Flags | Bit 0 `AUTH`: an HMAC tag follows the payload. Bit 1 `RETRANSMIT`. Other bits must be 0 |
| 5 | 1 | Reserved | Must be 0 |
| 6 | 4 | Peer ID | Random, non-zero, chosen per session with `secrets` |
| 10 | 4 | Sequence | Per-sender counter. Only DRAW and CLEAR consume a number. Other types carry the sender's *next* number without consuming it |
| 14 | 4 | Lamport | Sender's Lamport timestamp |
| 18 | 2 | Length | Payload bytes after the header (at most 1,200, excluding any HMAC tag) |

| Code | Type | Payload | Datagram size |
| --- | --- | --- | --- |
| 1 | `DRAW` | x `h`, y `h`, char `c`, colour `B` (6 bytes) | 26 bytes |
| 2 | `CLEAR` | none | 20 bytes |
| 3 | `STATE_REQ` | nonce `I` (4 bytes) | 24 bytes |
| 4 | `STATE_RESP` | 24-byte chunk header + 0–80 cell records of 14 bytes | 44–1,164 bytes |
| 5 | `HEARTBEAT` | none; the header's sequence field is the sender's next number | 20 bytes |
| 6 | `NACK` | target peer `I`, first seq `I`, count `H` | 30 bytes |
| 7 | `NOT_AVAILABLE` | first seq `I`, count `H`, oldest available seq `I` | 30 bytes |

`STATE_RESP` chunk header (`!IHHIHHII`): nonce, chunk index, chunk count,
CRC32 of the whole snapshot, canvas width, canvas height, clear-stamp
Lamport time, clear-stamp peer ID. Cell record (`!hhcBII`): x, y, char,
colour, Lamport time, peer ID. The largest datagram is 1,164 bytes,
1,212 bytes with an HMAC tag and the UDP and IPv4 headers, so it never
needs IP fragmentation on a 1,500-byte MTU. The roadmap's chunk header had
20 bytes; it gained width and height so a joiner can reject a snapshot
for a different canvas size.

Example: peer `1a2b3c4d` draws `#` at (20, 10) in colour 7, sequence `0x01020304`:

```text
CA 55 | 02 | 01 | 00 | 00 | 1A 2B 3C 4D | 01 02 03 04 | 00 00 00 00 | 00 06 | 00 14 00 0A 23 07
magic  ver DRAW flags rsvd  peer ID       sequence      lamport       length  x=20  y=10 '#' colour
```

The decoder rejects, with `ProtocolError`, any of the following:

- a wrong magic or version, or unknown flag bits
- a non-zero reserved byte, or peer ID 0
- a length field that does not match the bytes received, or a payload over
  1,200 bytes
- an unknown type, or a payload of the wrong size for its type
- invalid DRAW fields

### Peer identity

- Each v2 session picks a random 32-bit peer ID, shown on the top line of
  the app (`v2 peer 1a2b3c4d`).
- Duplicate and gap tracking (`tracker.py`) is keyed by peer ID instead of
  the source `(ip, port)`. A peer that changes source port is still the same
  sender, and two peers behind one address are separate senders.
- The v1 rule that a repeat after 2 s of silence means a restarted sender is
  turned off in v2. A restarted peer has a new ID, so a late duplicate stays
  a duplicate.
- A peer still ignores its own looped-back frames by source address. If a
  different address uses its peer ID, the peer keeps that message, picks a
  new ID, logs a warning and counts it in `peer_id_changes`.

### Lamport timestamps and conflict resolution (v2 only)

In v1, two users drawing on the same cell at the same moment can end up
with different canvases. In v2 every peer converges to the same canvas
once it has received the same events, whatever order they arrived in.

- **Clock (`lamport.py`):** each peer keeps a Lamport clock.
  - It ticks before every local DRAW or CLEAR.
  - On every received DRAW or CLEAR it becomes
    `max(own, received) + 1`.
  - So an edit made after seeing another peer's edit always has a larger
    timestamp, however many edits the other peer has made.
- **Stamp:** each event's stamp is `(lamport, peer ID)`, compared as a tuple.
  The peer ID only breaks exact ties.
- **Stamped before applied:** `CanvasApp` asks `session.Session` for the
  stamp first, then applies that exact event locally and sends it, so the
  local and remote copies are identical.
- **Per-cell rule (`canvas.py`):** every cell stores the stamp of its last
  write. A DRAW is applied only if its stamp is larger than the cell's stamp
  and larger than the canvas-wide clear stamp.
- **CLEAR:** a CLEAR raises the clear stamp and blanks only the cells written
  before it. A delayed CLEAR therefore never erases later drawing, and a
  delayed DRAW older than a CLEAR stays erased. A CLEAR older than the
  current clear stamp is ignored.
- **v1 unchanged:** events without a timestamp (v1 mode, `draw()`,
  `clear()`) behave exactly as before: they always apply, in arrival order,
  and leave the cell unstamped. Any stamped event beats an unstamped cell.
- **Persistence:** save files keep format version 1 without stamps. A
  loaded canvas is unstamped, and its clear stamp is reset.

During a true conflict a user may see their own character replaced by the
other peer's. That is the intended deterministic outcome, not data loss.
Convergence covers only events a peer has received. Lost events are
handled by late-join sync and loss recovery (below).

### Late-join synchronization (v2 only, `sync.py`)

A v2 app asks the group for the current canvas when it starts. It
transfers the canvas *state*, one record per cell, never the drawing
history.

1. **Request.** The new peer multicasts `STATE_REQ` with a random 32-bit
   nonce. Until a snapshot is merged it buffers incoming DRAW/CLEAR events
   instead of applying them. Its own local drawing is applied at once.
2. **Responders.** Every peer schedules a reply after a random delay:
   0–200 ms, or 200–400 ms while it is itself still waiting for a snapshot,
   so that a synced peer normally answers first. Two peers that start
   within a few seconds of each other can still exchange what they have.
   When the delay expires, the peer multicasts its whole canvas as
   `STATE_RESP` chunks of up to 80 cells:
   - every occupied cell, plus any blank cell that still carries a stamp;
   - the clear stamp.

   A peer that hears a chunk for that nonce first cancels its own reply.
   Each peer answers a nonce at most once.
3. **Joiner.** The joiner:
   - accepts chunks only for its own nonce and only from the first
     responder;
   - ignores exact duplicate chunks;
   - rejects a chunk that contradicts an earlier one;
   - once every chunk is present, checks the CRC32, the canvas size, the
     bounds and repeated cells.
4. **Merge.** The joiner merges the snapshot with the last-writer-wins
   rule, moves its Lamport clock past every timestamp in it, then replays
   the buffered events. A snapshot therefore cannot overwrite a newer
   event, and the result does not depend on arrival order. A cell with no
   stamp on the responder (for example loaded from a file) fills only a
   cell that is still blank and unstamped on the joiner.
5. **Failure.** A request with no complete, valid snapshot after 1 s
   (extended by 0.5 s after each new chunk) is repeated with a new nonce.
   Lost, missing, corrupt and wrong-size snapshots all end this way. After
   3 attempts the joiner gives up, replays its buffer and keeps its canvas.
   This is the normal outcome for the first peer in a group. The message
   line says what happened, and the status line shows `Sync 1/3`, `Synced`
   or `Sync none`.

Suppression is best-effort, as in SRM: peers whose timers fire within the
same 50 ms UI loop iteration may both answer. The joiner then uses the
first complete snapshot and ignores the rest.

### Packet-loss recovery (v2 only, `recovery.py`)

Recovery is receiver-initiated (NACK-based), in the style of SRM and NORM.
A loss-free network carries no extra traffic apart from one 20-byte
heartbeat per peer per second. It improves delivery, but cannot guarantee
it, because recovery messages use the same unreliable network.

- **Detecting loss.**
  - The tracker marks numbers skipped by a later event as missing.
  - Every peer multicasts a `HEARTBEAT` each second carrying its next
    sequence number. If the last events of a burst were lost and the sender
    then went quiet, the heartbeat exposes them.
- **Asking.**
  - A newly missing event waits a random 50–150 ms. Then one `NACK`
    requests each contiguous run (up to 256 numbers) from the original
    sender.
  - A NACK heard from another peer for the same event counts as our own
    request (suppression).
  - Requests are retried after 0.2 s and 0.4 s (exponential back-off), at
    most 3 NACKs in all.
- **Answering (network thread).**
  - Each peer keeps its last 512 sent events.
  - For a NACK naming it, a peer multicasts the original DRAW or CLEAR
    again with the `RETRANSMIT` flag, at most once per event every 0.2 s,
    and at most 256 events per NACK.
  - For events it no longer has, it sends one rate-limited `NOT_AVAILABLE`.
- **Giving up.**
  - An event is unrecoverable when the sender answers `NOT_AVAILABLE`, or
    when 3 NACKs have gone unanswered (about 1.5 s).
  - The receiver then stops asking for it and requests a canvas snapshot
    instead (late-join sync), at most once every 5 s.
  - The message line says how many events were lost. The status line shows
    `Rec n Lost m`.
- **No duplicates or loops.**
  - The tracker delivers each sequence number once, and drops later copies
    and retransmissions.
  - Retransmissions are never NACKed or retransmitted by other peers.
  - Last-writer-wins makes a late repair harmless even if newer events
    arrived first.

### Optional authentication (v2 only, `auth.py`)

`--auth` (with `--protocol 2`) turns on HMAC authentication with a
passphrase shared by the group. The passphrase is read from the
`TERMINAL_CANVAS_PASSPHRASE` environment variable if it is set, otherwise
typed at a hidden prompt. It is never logged or displayed, and neither is
the derived key.

- **Key:** `PBKDF2-HMAC-SHA256(passphrase, fixed salt, 200,000 iterations)`
  gives a 32-byte key. The salt is fixed because every peer must derive the
  same key without exchanging anything.
- **Tag:** every v2 datagram gets the `AUTH` flag and a 16-byte tag after
  the payload: the first 16 bytes of `HMAC-SHA256(key, header + payload)`.
  The length field still counts only the payload. A DRAW becomes 42 bytes,
  and the largest `STATE_RESP` 1,180 bytes.
- **Checking:** receivers check the tag with `hmac.compare_digest`
  (constant time) before trusting any other field. A missing tag, a wrong
  tag length, a wrong key, or any modified header, payload or tag byte is
  dropped and counted in `MulticastPeer.auth_failures`. The status line
  shows this as `AuthFail n`.
- **Without `--auth`:** v2 peers reject tagged frames. v1 is not affected,
  because authentication needs v2.

| Threat | Without `--auth` | With `--auth` |
| --- | --- | --- |
| Read the drawing traffic | Possible | **Still possible: there is no encryption** |
| Inject forged events | Possible | Rejected unless the sender knows the passphrase |
| Modify frames in transit | Possible | Detected and dropped |
| Replay captured frames | Possible | Recent replays are dropped as duplicates; old ones may be accepted |
| Impersonate a specific peer | Possible | Possible for anyone who knows the passphrase (one key for the group) |

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

### Protocol v2 modules

```python
protocol_v2.encode_message(message, key=None) -> bytes
protocol_v2.decode_message(data, key=None) -> dict   # LegacyFrameError, AuthenticationError
MulticastPeer(on_event, wire_version=2, peer_id=None, history_size=512,
              loss_rate=0.0, auth_key=None)          # .send(), .send_control(), .send_heartbeat()
Session(canvas, peer)        # .stamp(event), .receive(message), .start(), .tick(), .status()
auth.derive_key(passphrase) -> AuthKey
```

`CanvasApp(..., session=Session(...))` turns on v2 behaviour in the app.
Without a session, `CanvasApp` behaves exactly as in v1.

## Running tests

From the project root:

```sh
python -m compileall -q .
python -m unittest discover -s tests -v
```

The tests need no third-party packages, no terminal, no curses and no
external network. The suite has 396 tests:

| File | Tests | Covers |
| --- | --- | --- |
| `test_protocol.py` | 25 | v1 frame (unchanged) |
| `test_network.py` | 52 | v1 networking (unchanged) |
| `test_tracker.py` | 19 | duplicate and gap tracking (unchanged) |
| `test_canvas.py` | 19 | canvas state (unchanged) |
| `test_app.py` | 40 | UI logic, run loop, `main()` (unchanged) |
| `test_persistence.py` | 52 | Phase F: save/load (unchanged since Phase F) |
| `test_protocol_v2.py` | 25 | Phase A: v2 header, validation, peer IDs |
| `test_network_v2.py` | 27 | Phase A: v2 receive path, simulated and loopback v2 peers, `--protocol` |
| `test_lamport.py` | 31 | Phase D: clock, last-writer-wins, arrival-order independence |
| `test_sync.py` | 40 | Phase C: chunks, CRC, reassembly, timeouts, suppression, merging, late join |
| `test_recovery.py` | 35 | Phase B: heartbeat, NACK, retransmission, NOT_AVAILABLE, fallback, simulated loss |
| `test_auth.py` | 31 | Phase E: key derivation, tags, tampering, regressions with auth on |

Run one file with, for example, `python -m unittest tests.test_sync -v`.

`tests/simnet.py` is an in-memory multicast network used by the v2 tests.
It connects several real `MulticastPeer` objects through their normal
receive path. A test can:

- drop chosen datagrams for chosen receivers;
- shuffle or duplicate delivery with a seeded random generator;
- take the whole network down.

Sync and recovery timers are driven by a fake clock, so these tests are
deterministic and do not depend on real Wi-Fi loss.

**Multicast skips.** Thirteen tests use real multicast on this host:

- `LoopbackMulticastTests`
- `TwoAppLoopbackTests`
- `V2LoopbackMulticastTests`
- `LoopbackLateJoinTests`

The probe uses `INTERFACE_ADDRESS` from `config.py`. If that address is
not one of this computer's current addresses (it changes with the Wi-Fi
network), these tests are skipped, not failed. Set it to the current
address from `ipconfig` to run them.

| Kind | Where | Touches the network? |
| --- | --- | --- |
| Pure unit tests | `test_protocol.py`, `test_canvas.py`, `test_tracker.py`; app logic, run loop and `main()` cleanup in `test_app.py`; `HandleDatagramTests` (including duplicate and gap handling) and `PeerStateTests` in `test_network.py` | No |
| Mocked tests | `MockedSocketTests` and `MockedSpecificInterfaceTests` check socket options, membership join/leave, interface pinning, the receive thread and failure cleanup. `CursesViewTests` checks rendering and key translation with a fake curses module | No |
| Local multicast tests | `LoopbackMulticastTests` (peers), `TwoAppLoopbackTests` (two `CanvasApp`s), `V2LoopbackMulticastTests` and `LoopbackLateJoinTests` (v2). They use real multicast on this laptop, on a random port from 40000 to 49999 | Yes, this host only |
| Simulated-network tests | All v2 integration tests in `test_network_v2.py`, `test_lamport.py`, `test_sync.py`, `test_recovery.py` and `test_auth.py`, through `tests/simnet.py` | No |

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

- **v1 and v2 do not interoperate.** Every peer in a session must use the
  same `--protocol` (and, with `--auth`, the same passphrase).
- **The items below describe v1 (the default).** With `--protocol 2`:
  - lost events are recovered (Phase B);
  - concurrent edits converge (Phase D);
  - late joiners receive the canvas (Phase C);
  - `--auth` adds integrity (Phase E);
  - each frame carries a peer ID (Phase A).

  v2 has its own limits, listed after the v1 items.
- **UDP does not guarantee delivery.** Frames can be lost, duplicated or
  reordered, and v1 has no acknowledgements or retransmissions.
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
  character. v2 fixes this with Lamport timestamps and peer IDs.
- **No late-join synchronization in v1.** In the v1 frame,
  `STATE_REQ`/`STATE_RESP` are reserved type codes only. A v1 peer that
  joins late sees only events sent after it joined.
- **No authentication in v1, and no encryption in either version.** Any
  host on the LAN can read every frame. Without `--auth` it can also inject
  valid frames. The magic number is not a security measure.
- **No peer identity in the v1 frame.** A v1 sender is identified only by
  the source `(ip, port)` in `event["addr"]`, which changes when that peer
  restarts. Duplicate and gap tracking therefore works per peer *session*.
- **v2 limits:**
  - Recovery improves delivery but cannot guarantee it. An event lost by
    every peer, from a sender that has crashed, is gone.
  - Events reordered before the first frame heard from a peer are treated
    as old and dropped; a snapshot repairs the canvas.
  - Response and NACK suppression are best-effort.
  - Save files do not keep Lamport stamps.
  - Authentication uses one group key with a fixed salt, offers only
    limited replay protection, and no confidentiality.
  - The Lamport clock stops at 2³²−1.
  - None of this has been validated between two physical computers yet.
- **LAN only.** With TTL 1, frames are not routed beyond the local subnet.
- **The network can block multicast.** Wi-Fi client isolation, VPNs and
  managed switches without IGMP snooping may drop or flood multicast.
