# Live Demonstration Guide

**Real-Time Decentralized Collaborative Terminal Canvas over UDP Multicast**

*A Serverless Peer-to-Peer Group Communication System using IPv4 Multicast and UDP.*

This guide is for the faculty demonstration. Follow it top to bottom, from
a terminal. Every command and key below has been checked against the
current source code (`app.py`, `config.py`, `network.py`,
`protocol_v2.py`, `session.py`).

> **Read this first.** Every feature here has automated tests on one
> laptop (unit tests, simulated multi-peer networks, and real multicast
> loopback on the same machine). **Protocol v2 has not yet been validated
> between two physical laptops.** Rehearse the whole sequence on the real
> Windows and macOS laptops, on the network you will use, before the
> demonstration. Only present a feature as working once you have seen it
> work there.

---

## Contents

1. [Project overview](#1-project-overview)
2. [Prerequisites](#2-prerequisites)
3. [Launch commands (Windows and macOS)](#3-launch-commands-windows-and-macos)
4. [Faculty demonstration sequence](#4-faculty-demonstration-sequence)
5. [Authentication (optional)](#5-authentication-optional)
6. [Persistence](#6-persistence)
7. [Wireshark](#7-wireshark)
8. [Troubleshooting](#8-troubleshooting)
9. [Final demonstration checklist](#9-final-demonstration-checklist)
10. [Presentation notes](#10-presentation-notes)

---

## 1. Project overview

Every laptop runs the same program. Each peer:

- keeps its own copy of a 60 × 18 character canvas;
- is both a sender and a receiver.

There is **no server**. Drawing events are sent as compact binary frames
inside **UDP** datagrams to the **IPv4 multicast group `239.255.0.1`, port
`5005`**, with TTL 1 (local subnet only). The operating system joins the
group with `IP_ADD_MEMBERSHIP`, which produces **IGMP** membership
reports. IGMP manages membership only; it never carries drawing data.

| Feature | How to enable | Status |
| --- | --- | --- |
| Live collaborative drawing (DRAW, CLEAR) | always | Implemented |
| Protocol v1: 13-byte frame, duplicate and gap counting | default (`--protocol 1`) | Implemented |
| Protocol v2: 20-byte versioned header with a random peer ID | `--protocol 2` | Implemented |
| Lamport-clock conflict resolution (last writer wins, tie broken by peer ID) | v2 | Implemented |
| Late-join synchronization (STATE_REQ / STATE_RESP snapshots) | v2, automatic at start-up | Implemented |
| Packet-loss recovery (heartbeat, NACK, retransmission, snapshot fallback) | v2, automatic | Implemented |
| Simulated packet loss, for demonstrating recovery | `--simulate-loss PERCENT` | Implemented (demo aid) |
| HMAC-SHA256 message authentication | `--auth` (v2 only) | **Optional** |
| Local save and load of the canvas (JSON) | `S` / `L` keys, `--save` / `--load` | Implemented (local only) |

**Limitations to state honestly:**

- UDP multicast is best-effort. Recovery improves delivery but cannot
  guarantee it.
- v1 and v2 peers cannot talk to each other, and neither can peers with and
  without `--auth`.
- HMAC provides integrity and group authentication, **not encryption**.
  Anyone on the LAN can still read the frames in Wireshark.
- Saving and loading are local; a loaded file is not sent to other peers.
- With TTL 1, traffic stays on one subnet. The network must also allow
  multicast between clients.
- v2 has been validated on one laptop only.

---

## 2. Prerequisites

### Software

- **Python 3.10 or newer** on both laptops.
- **Windows** needs the `windows-curses` package (listed in
  `requirements.txt`), because Python on Windows has no `curses` module.
- **macOS** needs nothing extra: `curses` is part of Python there.
- **Optional:** Wireshark, on at least one laptop.
- **Terminal window:** at least **62 columns × 23 rows**. On Windows, use
  Windows Terminal rather than the classic console.

### Check the Python version

| Windows (PowerShell) | macOS (Terminal) |
| --- | --- |
| `python --version` | `python3 --version` |

### Enter the project directory

```powershell
# Windows (PowerShell)
cd <PATH_TO_PROJECT>\terminal-canvas
```

```sh
# macOS (Terminal)
cd <PATH_TO_PROJECT>/terminal-canvas
```

### Install dependencies

```powershell
# Windows: installs windows-curses
python -m pip install -r requirements.txt
```

```sh
# macOS: nothing extra is needed, so this installs nothing
python3 -m pip install -r requirements.txt
```

### Find the active Wi-Fi IPv4 address

| Windows (PowerShell) | macOS (Terminal) |
| --- | --- |
| `ipconfig` and read **IPv4 Address** under the **Wi-Fi** adapter | `ipconfig getifaddr en0` |

- **Verify the interface and address before every launch.** The address
  changes when the laptop reconnects to Wi-Fi.
- On Windows, ignore virtual adapters such as VirtualBox (`192.168.56.x`),
  Hyper-V, WSL or VPN adapters.
- On macOS, `en0` is usually Wi-Fi, but not always. If the command prints
  nothing, find the active interface and its `inet` address with
  `ifconfig`.
- Always pass the address with `--interface`. The default in `config.py`
  is a fixed address from the development laptop.

### Optional: confirm the build works on each laptop

```powershell
python -m unittest discover -s tests      # Windows
```

```sh
python3 -m unittest discover -s tests     # macOS
```

The multicast tests are skipped, not failed, if `config.py`'s address is
not one of the laptop's current addresses.

---

## 3. Launch commands (Windows and macOS)

The main demonstration uses **protocol v2 on both peers**. Replace the
placeholders with the addresses found in section 2.

**Windows (Peer 1):**

```powershell
python app.py --protocol 2 --interface <WINDOWS_IP>
```

**macOS (Peer 2):**

```sh
python3 app.py --protocol 2 --interface <MAC_IP>
```

**Rules:**

- **Same protocol everywhere.** A v1 peer drops every v2 frame and a v2
  peer drops every v1 frame, so mixed peers simply do not see each other.
- **Each laptop uses its own address.** `--interface` is this laptop's
  Wi-Fi IPv4 address, never the other laptop's.
- The group (`239.255.0.1`) and port (`5005`) are fixed in `config.py` and
  are the same on both laptops.

**Supported options** (from `python app.py --help`):

| Option | Meaning |
| --- | --- |
| `--interface ADDR` | IPv4 address of the network interface to use |
| `--protocol {1,2}` | Wire protocol; `1` is the default |
| `--simulate-loss PERCENT` | Drop 0–90 % of *received* datagrams on purpose (demo aid) |
| `--auth` | HMAC authentication (needs `--protocol 2`) |
| `--load FILE` | Load a saved canvas at start-up (local only) |
| `--save FILE` | Save the canvas to `FILE` on exit; `S` also saves there |

**What the screen shows:**

- **Top line:** the group, the interface, the sending address, and
  `v2 peer xxxxxxxx` (this session's random peer ID). If enabled, it also
  shows `SIMULATED LOSS n%` and `HMAC on`.
- **Status line:** brush, colour, pen, cursor `(x,y)`, `Sent`, `Recv`,
  `Miss`, `Dup`, and the v2 status: `Sync 1/3`, `Synced` or `Sync none`;
  `Rec n Lost m`; `AuthFail n`.
- **Bottom line:** messages, or the key help.

**Keys:**

| Key | Action |
| --- | --- |
| Arrow keys | Move the cursor |
| Space | Draw the brush character at the cursor |
| `p` | Toggle pen mode (draws while moving) |
| `e` | Erase the cell |
| `b`, then a character | Choose the brush character (Esc cancels) |
| `0`–`7` | Colour: default, red, green, yellow, blue, magenta, cyan, white |
| `X` (Shift+x) | Clear the canvas for all peers |
| `S` / `L` (Shift+s / Shift+l) | Save / load the canvas (local only) |
| `q` or Ctrl+C | Quit |

---

## 4. Faculty demonstration sequence

Do the steps in this order. Steps marked **(optional)** should only be
shown if they worked during rehearsal on these laptops and this network.

### Step 1: Introduce the project

- **Do:** show the architecture: two laptops, one multicast group, no
  server.
- **Observe:** nothing is running yet.
- **Demonstrates:** context.
- **Say:** "Each laptop runs the same program and keeps its own replica of
  the canvas. There is no server. Every drawing action is one small UDP
  datagram sent to the IPv4 multicast group 239.255.0.1 on port 5005. The
  network delivers it to every member of the group."

### Step 2: Start Peer 1 on Windows

- **Do:**
  ```powershell
  python app.py --protocol 2 --interface <WINDOWS_IP>
  ```
- **Observe:**
  - The top line shows the group, the interface and `v2 peer xxxxxxxx`.
  - The message line shows `Requesting the canvas from other peers...`.
  - About 3 seconds later it shows `Canvas sync gave up (no peer
    answered); keeping this canvas`, and the status line shows
    `Sync none`.
- **Demonstrates:** joining the group (IGMP join), the random peer ID, and
  a first peer handling "nobody else is here".
- **Say:** "On start-up the peer joins the multicast group, picks a random
  32-bit peer ID, and asks the group for the current canvas. It is the
  first peer, so nobody answers. After three attempts it starts with an
  empty canvas. That is the expected outcome here, not an error."

### Step 3: Start Peer 2 on macOS

- **Do:**
  ```sh
  python3 app.py --protocol 2 --interface <MAC_IP>
  ```
- **Observe:** the macOS peer has its own peer ID. Its status line shows
  `Synced`: it received Peer 1's (empty) canvas.
- **Demonstrates:** late-join synchronization between two machines.
- **Say:** "The second peer asked for the canvas and Peer 1 answered with
  a snapshot. Both replicas now start from the same state."

### Step 4: Draw from Windows to Mac

- **Do:** on Windows, move with the arrow keys and press Space a few times.
  Press `p` and move to draw a line, then `p` again to stop.
- **Observe:** the drawing appears on the Mac almost immediately.
  - On Windows, `Sent` increases.
  - On the Mac, `Recv` increases.
- **Demonstrates:** UDP multicast delivery, from one sender to every group
  member.
- **Say:** "Each keystroke draws locally first, then sends one 26-byte v2
  message to the group. The Mac receives it on a background thread and
  applies it to its own replica."

### Step 5: Draw from Mac to Windows

- **Do:** on the Mac, press `b` then `@` (brush `@`), and draw a few cells.
- **Observe:** the `@` cells appear on Windows. Both sides now show both
  drawings.
- **Demonstrates:** symmetric peer-to-peer communication. Every peer is
  both a sender and a receiver.
- **Say:** "No peer is special. The Mac sends to the same group, and
  Windows receives it."

### Step 6: Colour changes and erasing

- **Do:**
  - On either laptop, press `1` (red) or `4` (blue) and draw.
  - Move onto a drawn cell and press `e` to erase it.
  - Optionally press `X` to clear the whole canvas on both laptops.
- **Observe:** colours match on both screens, and the erased cell is blank
  on both. After `X`, both canvases are empty and the message line shows
  `Canvas cleared for all peers`.
- **Demonstrates:** the DRAW message carries the character and colour; an
  erase is a DRAW of a space; CLEAR is a group-wide message.
- **Say:** "Colour is a field in the message. Erasing is drawing a blank
  character. Clear is its own message type, and it is ordered like any
  other edit."

### Step 7: Late-join synchronization

- **Do:**
  1. Draw something recognisable on Windows, for example your initials.
  2. On the Mac, press `q` to quit.
  3. While the Mac is offline, draw more on Windows.
  4. Restart the Mac peer with the same command as in Step 3.
- **Observe:** the restarted Mac peer shows `Canvas synchronised from peer
  xxxxxxxx (… cells, … new)` and `Synced`. Everything drawn on Windows,
  including what was drawn while the Mac was offline, appears at once.
- **Demonstrates:** late-join synchronization with `STATE_REQ` and
  `STATE_RESP`.
- **Say:**
  - "A new peer multicasts a state request with a random nonce. An
    existing peer waits a random 0–200 milliseconds, then answers with the
    current canvas state, split into chunks of up to 80 cells and checked
    with CRC32. Other peers that hear the answer stay quiet."
  - "The snapshot carries the current state, not the drawing history.
    Live events that arrive during the transfer are buffered and merged
    afterwards."

### Step 8: Conflict resolution (optional)

- **Do:**
  1. On both laptops, move the cursor to the same cell. Use the `(x,y)`
     shown on each status line.
  2. Choose different brushes, for example `b` `x` on Windows and `b` `o`
     on the Mac.
  3. Count down and press Space on both at the same moment.
- **Observe:** both screens end up showing the **same** character in that
  cell. Which one wins is decided by the rule, not by who pressed first.
- **Demonstrates:** Lamport clocks and deterministic last-writer-wins.
- **Say:**
  - "Each edit carries a Lamport timestamp and the sender's peer ID. A cell
    keeps the edit with the larger timestamp; the peer ID breaks a tie. An
    edit made after seeing someone else's edit always has a larger
    timestamp. So every replica reaches the same result, whatever order
    the packets arrived in."
  - "In protocol v1, this exact case could leave the two laptops showing
    different characters."
- **Note:** true simultaneity is hard by hand. If the presses are not
  simultaneous, the later edit wins on both screens, which still shows
  that the replicas agree.

### Step 9: Packet-loss recovery (optional)

- **Do:**
  1. On the Mac, press `q`.
  2. Restart the Mac peer with simulated loss:
     ```sh
     python3 app.py --protocol 2 --interface <MAC_IP> --simulate-loss 30
     ```
  3. On Windows, press `p` and draw quickly across many cells for about 10
     seconds.
- **Observe:**
  - The Mac's top line shows `SIMULATED LOSS 30%`.
  - Its status line shows `Rec n Lost m` once losses occur.
  - After a moment, the Mac's canvas matches the Windows canvas.
- **Demonstrates:** receiver-initiated (NACK-based) recovery.
- **Say:**
  - "The Mac deliberately throws away 30 percent of what it receives. Each
    peer numbers its events, so the receiver sees the gaps. A heartbeat
    every second also reveals a lost final event."
  - "The receiver waits a random 50–150 milliseconds and multicasts a NACK.
    The original sender retransmits from its history of 512 events. Retries
    use exponential back-off, with at most three NACKs per event."
  - "If an event can no longer be recovered, the receiver falls back to
    requesting a snapshot. It is in the style of SRM and NORM: there is no
    acknowledgement traffic when nothing is lost."
- **Note:** the simulated loss affects only what *this* peer receives. Quit
  and restart without `--simulate-loss` before the next step.

### Step 10: Authentication (optional, only if configured)

Use the commands in [section 5](#5-authentication-optional) on **both**
laptops, with the same passphrase.

- **Observe:**
  - Both top lines show `HMAC on`, and drawing works as before.
  - *(Optional contrast:)* if one laptop is restarted without `--auth`, the
    two stop exchanging drawings, and `AuthFail n` appears on the status
    line.
- **Demonstrates:** HMAC-SHA256 message integrity and group
  authentication.
- **Say:** "Every v2 message now carries a 16-byte HMAC tag, computed with
  a key derived from a shared passphrase. A peer without the right key
  cannot inject or modify drawing messages. The frames are still readable,
  because this is authentication, not encryption."

### Step 11: Local save and load

- **Do:**
  1. On Windows, press `S`. The message line shows `Saved to canvas.json`.
  2. Draw a few extra cells, or press `X`.
  3. Press `L`.
- **Observe:**
  - The Windows canvas returns to the saved drawing. The message line
    shows `Loaded canvas.json (local only; peers not updated)`.
  - The Mac does **not** change.
- **Demonstrates:** local JSON persistence with atomic writes, kept
  separate from the network.
- **Say:** "Saving writes a versioned JSON file atomically: a temporary
  file is replaced in a single step. Loading changes only this peer's
  canvas. It is deliberately not broadcast to other peers."

### Step 12: Wireshark multicast traffic

- **Do:** on the laptop running Wireshark, capture on the **Wi-Fi**
  interface with the display filter from [section 7](#7-wireshark). Draw a
  few cells.
- **Observe:**
  - **IGMP** membership reports for `239.255.0.1` when a peer starts or
    quits.
  - **UDP** datagrams to `239.255.0.1:5005`:
    - one per drawing action;
    - one heartbeat per peer per second;
    - larger `STATE_RESP` datagrams when a peer joins.
  - The UDP payload starts with `ca 55 02`.
- **Demonstrates:** that the application traffic really is IPv4 multicast
  over UDP, with IGMP managing group membership.
- **Say:** "Every frame starts with our magic number `0xCA55` and the
  version byte 2. The IGMP reports show the operating system joining and
  leaving the group. IGMP manages membership; drawing data travels only in
  UDP."

### Step 13: Conclusion

- **Do:** quit both peers with `q`.
- **Observe:** the terminal returns to normal. Wireshark shows the IGMP
  leave reports.
- **Say:** the closing explanation in
  [section 10](#10-presentation-notes).

---

## 5. Authentication (optional)

- **Optional:** authentication is off by default.
- **Protocol v2 only:** `--auth` without `--protocol 2` exits with an
  error.
- **Both peers** must use `--auth` **and the same passphrase**. Otherwise
  they cannot exchange any messages; `AuthFail n` counts the rejected
  frames.
- **What HMAC is:** message **authentication and integrity** with one key
  for the whole group. It is **not encryption**: frames remain readable in
  Wireshark. Any holder of the passphrase can send as any peer.
- **Keep the passphrase private.** Use a fresh demo passphrase. Never
  write a real one in this file, in commits, in screenshots or on slides.
  The passphrase is read from the `TERMINAL_CANVAS_PASSPHRASE` environment
  variable if it is set, otherwise from a hidden prompt. It is never
  displayed or logged.

**Recommended: the hidden prompt**, which leaves the passphrase out of the
shell history.

```powershell
# Windows (PowerShell)
python app.py --protocol 2 --auth --interface <WINDOWS_IP>
# then type the shared passphrase at "Shared passphrase:"
```

```sh
# macOS (Terminal)
python3 app.py --protocol 2 --auth --interface <MAC_IP>
# then type the shared passphrase at "Shared passphrase:"
```

**Alternative: the environment variable** (it may be saved in shell
history; remove it afterwards).

```powershell
# Windows (PowerShell)
$env:TERMINAL_CANVAS_PASSPHRASE = "<SHARED_DEMO_PASSPHRASE>"
python app.py --protocol 2 --auth --interface <WINDOWS_IP>
Remove-Item Env:TERMINAL_CANVAS_PASSPHRASE
```

```sh
# macOS (Terminal)
export TERMINAL_CANVAS_PASSPHRASE='<SHARED_DEMO_PASSPHRASE>'
python3 app.py --protocol 2 --auth --interface <MAC_IP>
unset TERMINAL_CANVAS_PASSPHRASE
```

An empty passphrase is rejected and the program exits.

---

## 6. Persistence

| Control | Behaviour |
| --- | --- |
| `S` | Save this peer's canvas to a JSON file |
| `L` | Replace this peer's canvas with the saved file |
| `--save FILE` | Save to `FILE` automatically when the app exits; `S` also saves there |
| `--load FILE` | Load `FILE` at start-up. If it cannot be loaded, the app explains why and exits before joining the group |

**Which file `S` and `L` use:**

| Options given | `S` saves to | `L` loads from |
| --- | --- | --- |
| none | `canvas.json` in the current folder | `canvas.json` |
| `--load A` | `A` | `A` |
| `--save B` | `B` | `B` |
| `--load A --save B` | `B` | `A` |

```powershell
# Windows
python app.py --protocol 2 --interface <WINDOWS_IP> --load demo.json --save demo.json
```

```sh
# macOS
python3 app.py --protocol 2 --interface <MAC_IP> --load demo.json --save demo.json
```

- **Local only.** Loading replaces this peer's canvas and sends nothing.
  Other peers keep their own canvases. To share a drawing that way, every
  peer loads the same file.
- **Atomic and validated.**
  - A failed save leaves the previous file intact.
  - A corrupt, wrong-size or wrong-version file is rejected, and the
    canvas is left unchanged.
- **Format:** JSON version 1, holding only the occupied cells. Lamport
  stamps are not saved, so after loading, any later edit from a peer
  replaces a loaded cell.

---

## 7. Wireshark

- **Capture interface:** the laptop's **Wi-Fi** adapter, the same one
  passed to `--interface`.
- **Display filter:**

  ```text
  ip.dst == 239.255.0.1 || igmp
  ```

- **Multicast destination:** `239.255.0.1`, UDP port `5005`, TTL 1.

| What you see | What it demonstrates |
| --- | --- |
| IGMP membership reports for `239.255.0.1` | The host joining (and, on exit, leaving) the multicast group. Switches and routers use these to decide where to forward the group's traffic |
| UDP to `239.255.0.1:5005`, payload starting `ca 55 02` | Application data: protocol v2 frames (magic `0xCA55`, version 2) |
| UDP length 34 (26-byte payload) | One v2 DRAW. With `--auth`: 50 (42-byte payload) |
| UDP length 28, once per second per peer | HEARTBEAT (20-byte payload; used to detect a lost final event) |
| UDP length 32 / up to 1,172 | STATE_REQ / STATE_RESP chunks during a late join |
| UDP length 38 | NACK or NOT_AVAILABLE, during loss recovery |
| UDP length 21, payload `ca 55 01 …` | A protocol **v1** DRAW or CLEAR (if a peer runs without `--protocol 2`) |

**Visibility depends on the setup:**

- A laptop always sees the traffic it sends.
- It sees the other laptop's traffic only if the network forwards
  multicast between clients.
- Some Wi-Fi drivers on Windows do not show all traffic.
- If nothing appears, see [section 8](#8-troubleshooting).

---

## 8. Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `Could not join 239.255.0.1:5005 on interface …` at start-up | Wrong `--interface` address | Run `ipconfig` (Windows) or `ipconfig getifaddr en0` / `ifconfig` (macOS) again. Use this laptop's current Wi-Fi IPv4 address |
| Starts, but nothing arrives from the other laptop | The wrong interface was chosen (VPN or virtual adapter), or multicast is blocked | Check the address. Disable VPNs (for example Cloudflare WARP or McAfee VPN) during the demo |
| Peers see no drawings from each other, although both started without errors | **Protocol mismatch**: one peer runs v1 (the default) | Start **both** with `--protocol 2`. A v2 peer counts and drops v1 frames, and logs a warning to `terminal_canvas.log` |
| Drawing works in one direction only | **Windows Firewall** is blocking inbound UDP on the receiving laptop | Allow Python on **Private** networks when Windows asks. Make sure the Wi-Fi network is marked Private, or add an inbound rule for UDP port 5005. *(Unverified on the demo machine.)* For example, in an administrator PowerShell: `New-NetFirewallRule -DisplayName "Terminal Canvas UDP 5005" -Direction Inbound -Protocol UDP -LocalPort 5005 -Action Allow -Profile Private` |
| macOS receives nothing | **macOS firewall** is blocking incoming connections for Python | When macOS asks, allow incoming connections for Python. Otherwise check System Settings → Network → Firewall → Options. *(Not verified on the demo Mac.)* |
| Nothing arrives in either direction on campus Wi-Fi | **Client isolation or multicast filtering** on the access point | Use a phone hotspot or a home router known to allow client-to-client traffic, or connect both laptops by Ethernet |
| `AuthFail n` increases and no drawings arrive | **Authentication mismatch**: `--auth` on one peer only, or different passphrases | Restart both peers with `--auth` and the same passphrase, or both without `--auth`. The status counter is currently the only on-screen sign of the mismatch |
| A late joiner shows `Canvas sync gave up (no peer answered)` although the other peer is running | Multicast is not getting through, or there is a protocol or `--auth` mismatch | Check that drawing works in both directions first (Steps 4–5), and that both peers use the same `--protocol` and `--auth` setting. Then quit and restart the joiner |
| Canvases differ after heavy drawing | Loss larger than recovery could repair, or `--simulate-loss` still active | Wait a few seconds: unrecoverable events trigger a snapshot fallback within about 5 s. Otherwise restart the affected peer (it re-syncs at start-up). Remove `--simulate-loss` |
| `Terminal too small: need 62x23` | Window too small | Enlarge the terminal window, or reduce the font size |
| App seems frozen in a classic Windows console | A click started text selection (the window title begins with "Select") | Press Esc. Use Windows Terminal instead |
| Wireshark shows no packets | Wrong capture interface or filter, or the traffic is blocked | Capture on the Wi-Fi adapter used by `--interface`. Check the filter `ip.dst == 239.255.0.1 \|\| igmp`. Draw something to generate traffic. If only the local laptop's packets appear, the network is not forwarding multicast between clients |

The app writes warnings and errors to `terminal_canvas.log` in the
current folder. The file is created only if something is logged.

---

## 9. Final demonstration checklist

- [ ] Both laptops have Python 3.10 or newer (`python --version` / `python3 --version`).
- [ ] Windows has `windows-curses` installed (`python -m pip install -r requirements.txt`).
- [ ] The same version of the project folder is on both laptops.
- [ ] Both laptops are on the **same network**, and it allows client-to-client multicast. A hotspot or home router is safer than campus Wi-Fi.
- [ ] VPN clients are disabled.
- [ ] The current Wi-Fi IPv4 address of each laptop has been written down (`ipconfig` / `ipconfig getifaddr en0`).
- [ ] Firewalls allow Python and inbound UDP 5005 on both laptops.
- [ ] Terminal windows are at least 62 × 23 (Windows Terminal on Windows).
- [ ] Both peers start with `--protocol 2` and their own `--interface` address.
- [ ] Drawing works **both ways**, and late join works: rehearsed on these laptops, on this network.
- [ ] Optional demos (conflict, loss recovery, authentication) were rehearsed. Only those that worked are in the plan.
- [ ] If authentication will be shown: a fresh demo passphrase is agreed, and it is not written anywhere visible.
- [ ] Wireshark is installed, capturing on the Wi-Fi interface, with the filter `ip.dst == 239.255.0.1 || igmp` ready.
- [ ] Any old `canvas.json` or demo file that should not be loaded by accident is moved away.
- [ ] Both peers are quit with `q` at the end, so the IGMP leave reports appear.

---

## 10. Presentation notes

### Opening (about 30 seconds)

"Our project is a real-time collaborative drawing canvas that runs in the
terminal, with no server at all. Every laptop is an equal peer. When
someone draws, the program sends one small UDP datagram to the IPv4
multicast group 239.255.0.1, and the network delivers it to every laptop
that has joined the group through IGMP. On top of that basic system we
built a second protocol version. It adds:

- peer identity;
- Lamport-clock conflict resolution, so every replica agrees;
- late-join synchronization, so a new peer receives the current canvas;
- NACK-based packet-loss recovery;
- optional HMAC authentication."

### Closing (about 30 seconds)

"We showed decentralized group communication: one multicast transmission
reaches every member, and IGMP manages the membership. UDP is
best-effort, so reliability and consistency are handled at the
application layer:

- sequence numbers and heartbeats to detect loss;
- receiver-initiated NACKs to recover it;
- snapshots for late joiners;
- Lamport timestamps so all replicas converge.

The design is still serverless: every peer runs the same rules
independently. The limits are deliberate and known. Delivery is improved,
not guaranteed. Authentication protects integrity but does not encrypt.
Traffic stays on one subnet because of TTL 1. Saving and loading are
local."
