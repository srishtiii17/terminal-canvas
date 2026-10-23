"""Shared networking configuration for the terminal canvas."""

# IPv4 Class D group in the administratively scoped range 239.0.0.0/8,
# as specified in the project presentation.
MULTICAST_GROUP = "239.255.0.1"
MULTICAST_PORT = 5005

# TTL of 1 keeps datagrams on the local subnet (the LAN).
MULTICAST_TTL = 1

# Deliver multicast sent from this host back to this host, so several peers
# can run on one machine. Each peer still ignores the frames it sent itself.
MULTICAST_LOOPBACK = True

# Local interface used to join the group and send. "0.0.0.0" lets the OS choose.
INTERFACE_ADDRESS = "172.17.83.158"

# Fits any IPv4 UDP payload (max 65507 bytes), so an oversized datagram is
# read whole and rejected by the decoder. With a smaller buffer Windows raises
# WSAEMSGSIZE instead of truncating.
RECV_BUFFER_SIZE = 65535

# How often the receive loop wakes up to check whether it should stop.
SOCKET_TIMEOUT = 0.2

# Shared canvas size in cells. Every peer must use the same values so that
# coordinates mean the same cell everywhere; it is independent of terminal
# size. 60x18 plus the border and status lines fits an 80x24 terminal.
CANVAS_WIDTH = 60
CANVAS_HEIGHT = 18
