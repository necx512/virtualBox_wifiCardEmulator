#!/usr/bin/env python3
"""
hwsim_relay.py — Bridge mac80211_hwsim frames between two VMs over UDP.

AP  (radios=2, hostapd on wlan1):
  ./hwsim_relay.py --peer 10.0.0.2 --local-mac 02:00:00:00:01:00

STA (radios=1):
  ./hwsim_relay.py --peer 10.0.0.1
"""

import socket
import struct
import select
import argparse
import os
import sys

# ── Netlink constants ──────────────────────────────────────────────
NETLINK_GENERIC = 16
NLM_F_REQUEST = 0x01
NLM_F_ACK = 0x04

# Generic netlink controller
GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2
CTRL_ATTR_MCAST_GROUPS = 7
CTRL_ATTR_MCAST_GRP_NAME = 1
CTRL_ATTR_MCAST_GRP_ID = 2

# Netlink socket options
SOL_NETLINK = 270
NETLINK_ADD_MEMBERSHIP = 1

# ── mac80211_hwsim constants (from linux/mac80211_hwsim.h) ────────
HWSIM_CMD_REGISTER = 1
HWSIM_CMD_FRAME = 2
HWSIM_CMD_TX_INFO_FRAME = 3

HWSIM_ATTR_ADDR_RECEIVER = 1
HWSIM_ATTR_ADDR_TRANSMITTER = 2
HWSIM_ATTR_FRAME = 3
HWSIM_ATTR_FLAGS = 4
HWSIM_ATTR_RX_RATE = 5
HWSIM_ATTR_SIGNAL = 6
HWSIM_ATTR_TX_INFO = 7
HWSIM_ATTR_COOKIE = 8
HWSIM_ATTR_FREQ = 19

UDP_PORT = 5555
SIMULATED_SIGNAL = -30  # dBm, "perfect" signal
HWSIM_TX_STAT_ACK = (1 << 2)  # Bit 2: frame was acknowledged


# ── Netlink helpers ───────────────────────────────────────────────

def nlmsg_align(length):
    return (length + 3) & ~3


def build_nlattr(attr_type, data):
    """Build a single netlink attribute (nla_hdr + payload + padding)."""
    nla_len = 4 + len(data)
    attr = struct.pack("HH", nla_len, attr_type) + data
    # pad to 4-byte alignment
    attr += b'\x00' * (nlmsg_align(nla_len) - nla_len)
    return attr


def parse_nlattrs(data):
    """Parse netlink attributes from a buffer. Returns {type: bytes}."""
    attrs = {}
    offset = 0
    while offset + 4 <= len(data):
        nla_len, nla_type = struct.unpack("HH", data[offset:offset+4])
        if nla_len < 4:
            break
        payload = data[offset+4:offset+nla_len]
        attrs[nla_type] = payload
        offset += nlmsg_align(nla_len)
    return attrs


def build_genl_msg(family_id, cmd, attrs_bytes, seq=0):
    """Build a complete generic netlink message."""
    # genlmsghdr: cmd(1) + version(1) + reserved(2)
    genl_hdr = struct.pack("BBH", cmd, 1, 0)
    payload = genl_hdr + attrs_bytes

    # nlmsghdr: len(4) + type(2) + flags(2) + seq(4) + pid(4)
    msg_len = 16 + len(payload)
    nl_hdr = struct.pack("IHHII", msg_len, family_id, NLM_F_REQUEST, seq, 0)
    return nl_hdr + payload


# ── Resolve MAC80211_HWSIM family ─────────────────────────────────

def resolve_hwsim_family(nl_sock):
    """
    Query the generic netlink controller for MAC80211_HWSIM.
    Returns (family_id, mcast_group_id).
    """
    family_name = b"MAC80211_HWSIM\x00"
    attr = build_nlattr(CTRL_ATTR_FAMILY_NAME, family_name)
    msg = build_genl_msg(GENL_ID_CTRL, CTRL_CMD_GETFAMILY, attr, seq=1)

    nl_sock.send(msg)
    resp = nl_sock.recv(65536)

    # Parse nlmsghdr
    msg_len, msg_type, flags, seq, pid = struct.unpack("IHHII", resp[:16])
    # Skip nlmsghdr(16) + genlmsghdr(4)
    attrs = parse_nlattrs(resp[20:msg_len])

    family_id = struct.unpack("H", attrs[CTRL_ATTR_FAMILY_ID])[0]

    # Find the "config" multicast group
    mcast_grp_id = None
    if CTRL_ATTR_MCAST_GROUPS in attrs:
        grp_data = attrs[CTRL_ATTR_MCAST_GROUPS]
        # Nested attributes: each group is itself a nested attr
        groups = parse_nlattrs(grp_data)
        for _, grp_bytes in groups.items():
            grp_attrs = parse_nlattrs(grp_bytes)
            name = grp_attrs.get(CTRL_ATTR_MCAST_GRP_NAME, b"")
            name = name.rstrip(b'\x00').decode()
            if name == "config":
                mcast_grp_id = struct.unpack("I", grp_attrs[CTRL_ATTR_MCAST_GRP_ID])[0]
                break

    if family_id is None or mcast_grp_id is None:
        raise RuntimeError("Could not resolve MAC80211_HWSIM family. Is the module loaded?")

    return family_id, mcast_grp_id


# ── Get local hwsim radio MAC ────────────────────────────────────

def get_local_hwsim_mac(phy=None):
    """Read the hwsim radio's second address from sysfs.
    The kernel matches ADDR_RECEIVER against addresses[1] (the 0x40 variant)."""
    phys = sorted(os.listdir("/sys/class/ieee80211/"))
    if phy:
        phys = [p for p in phys if p == phy]
    for p in phys:
        addr_file = f"/sys/class/ieee80211/{p}/addresses"
        if os.path.exists(addr_file):
            with open(addr_file) as f:
                lines = f.read().strip().split("\n")
                # Use the second address (addresses[1]) — the one the kernel matches
                mac_str = lines[1] if len(lines) > 1 else lines[0]
                return bytes(int(b, 16) for b in mac_str.split(":"))
    raise RuntimeError("No hwsim radio found in /sys/class/ieee80211/")


# ── Frame relay logic ─────────────────────────────────────────────

def serialize_frame(attrs):
    """
    Pack the relevant hwsim attributes into a UDP payload.
    Format: [attr_count(2)] + [type(2) + len(4) + data]...
    """
    keys = [HWSIM_ATTR_ADDR_TRANSMITTER, HWSIM_ATTR_FRAME,
            HWSIM_ATTR_FLAGS, HWSIM_ATTR_COOKIE, HWSIM_ATTR_FREQ,
            HWSIM_ATTR_TX_INFO]
    parts = []
    count = 0
    for k in keys:
        if k in attrs:
            v = attrs[k]
            parts.append(struct.pack("<HI", k, len(v)) + v)
            count += 1
    return struct.pack("<H", count) + b"".join(parts)


def deserialize_frame(data):
    """Unpack UDP payload back into attribute dict."""
    attrs = {}
    offset = 0
    count = struct.unpack("<H", data[offset:offset+2])[0]
    offset += 2
    for _ in range(count):
        attr_type, attr_len = struct.unpack("<HI", data[offset:offset+6])
        offset += 6
        attrs[attr_type] = data[offset:offset+attr_len]
        offset += attr_len
    return attrs


def inject_frame(nl_sock, family_id, local_mac, attrs):
    """
    Inject a received frame into the local hwsim radio via netlink.
    """
    nl_attrs = b""
    nl_attrs += build_nlattr(HWSIM_ATTR_ADDR_RECEIVER, local_mac)

    if HWSIM_ATTR_ADDR_TRANSMITTER in attrs:
        nl_attrs += build_nlattr(HWSIM_ATTR_ADDR_TRANSMITTER,
                                 attrs[HWSIM_ATTR_ADDR_TRANSMITTER])
    if HWSIM_ATTR_FRAME in attrs:
        nl_attrs += build_nlattr(HWSIM_ATTR_FRAME, attrs[HWSIM_ATTR_FRAME])
    if HWSIM_ATTR_FLAGS in attrs:
        nl_attrs += build_nlattr(HWSIM_ATTR_FLAGS, attrs[HWSIM_ATTR_FLAGS])
    if HWSIM_ATTR_FREQ in attrs:
        nl_attrs += build_nlattr(HWSIM_ATTR_FREQ, attrs[HWSIM_ATTR_FREQ])

    # Set signal and rate for the "received" frame
    nl_attrs += build_nlattr(HWSIM_ATTR_SIGNAL, struct.pack("i", SIMULATED_SIGNAL))
    nl_attrs += build_nlattr(HWSIM_ATTR_RX_RATE, struct.pack("I", 1))

    msg = build_genl_msg(family_id, HWSIM_CMD_FRAME, nl_attrs)
    nl_sock.send(msg)


def send_tx_ack(nl_sock, family_id, attrs):
    """
    Send TX status back to the kernel so mac80211 doesn't think
    frames are lost (prevents retransmissions and timeouts).
    """
    nl_attrs = b""

    if HWSIM_ATTR_ADDR_TRANSMITTER in attrs:
        nl_attrs += build_nlattr(HWSIM_ATTR_ADDR_TRANSMITTER,
                                 attrs[HWSIM_ATTR_ADDR_TRANSMITTER])

    # Set ACK bit to indicate successful delivery
    if HWSIM_ATTR_FLAGS in attrs:
        flag_val = struct.unpack("<I", attrs[HWSIM_ATTR_FLAGS])[0]
    else:
        flag_val = 0
    flag_val |= HWSIM_TX_STAT_ACK
    nl_attrs += build_nlattr(HWSIM_ATTR_FLAGS, struct.pack("<I", flag_val))
    if HWSIM_ATTR_COOKIE in attrs:
        nl_attrs += build_nlattr(HWSIM_ATTR_COOKIE, attrs[HWSIM_ATTR_COOKIE])
    if HWSIM_ATTR_TX_INFO in attrs:
        nl_attrs += build_nlattr(HWSIM_ATTR_TX_INFO, attrs[HWSIM_ATTR_TX_INFO])

    nl_attrs += build_nlattr(HWSIM_ATTR_SIGNAL, struct.pack("i", SIMULATED_SIGNAL))

    msg = build_genl_msg(family_id, HWSIM_CMD_TX_INFO_FRAME, nl_attrs)
    nl_sock.send(msg)


# ── Main loop ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="mac80211_hwsim cross-VM relay")
    parser.add_argument("--peer", required=True, help="Peer VM IP address")
    parser.add_argument("--port", type=int, default=UDP_PORT, help="UDP port")
    parser.add_argument("--local-mac", default=None,
                        help="Override local radio MAC (e.g. 02:00:00:00:01:00). "
                             "Needed when AP has radios=2 and uses the second radio.")
    args = parser.parse_args()

    print(f"[*] Starting hwsim relay, peer={args.peer}:{args.port}")

    # 1. Open generic netlink socket
    nl_sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
    nl_sock.bind((0, 0))

    # 2. Resolve MAC80211_HWSIM family and multicast group
    family_id, mcast_grp_id = resolve_hwsim_family(nl_sock)
    print(f"[+] MAC80211_HWSIM family_id={family_id}, mcast_group={mcast_grp_id}")

    # 3. Subscribe to the multicast group to receive TX events
    nl_sock.setsockopt(SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, mcast_grp_id)

    # 4. Register as the wireless medium (required on kernel 5.x+)
    # Without this, the kernel delivers frames internally and never posts to netlink
    reg_msg = build_genl_msg(family_id, HWSIM_CMD_REGISTER, b"", seq=2)
    nl_sock.send(reg_msg)
    print("[+] Registered as hwsim medium")

    # 5. Get local radio MAC for frame injection
    if args.local_mac:
        local_mac = bytes(int(b, 16) for b in args.local_mac.split(":"))
    else:
        local_mac = get_local_hwsim_mac()
    print(f"[+] Local radio MAC: {':'.join(f'{b:02x}' for b in local_mac)}")

    # 6. Open UDP socket
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind(("0.0.0.0", args.port))
    peer_addr = (args.peer, args.port)

    print(f"[+] Relay running. Ctrl+C to stop.")
    tx_count = 0
    rx_count = 0

    try:
        while True:
            readable, _, _ = select.select([nl_sock, udp_sock], [], [], 1.0)

            for sock in readable:
                if sock is nl_sock:
                    # ── Outgoing: kernel TX → forward to peer ──
                    data = nl_sock.recv(65536)
                    if len(data) < 20:
                        continue

                    msg_len, msg_type, flags, seq, pid = struct.unpack("IHHII", data[:16])
                    if msg_type != family_id:
                        continue

                    cmd = data[16]  # genlmsghdr.cmd
                    if cmd != HWSIM_CMD_FRAME:
                        continue

                    # Parse attributes (skip nlmsghdr(16) + genlmsghdr(4))
                    attrs = parse_nlattrs(data[20:msg_len])

                    # Send TX ack back to kernel immediately
                    send_tx_ack(nl_sock, family_id, attrs)

                    # Forward frame to peer
                    payload = serialize_frame(attrs)
                    udp_sock.sendto(payload, peer_addr)

                    tx_count += 1
                    if tx_count % 100 == 0:
                        print(f"[>] TX: {tx_count} frames forwarded")

                elif sock is udp_sock:
                    # ── Incoming: peer frame → inject locally ──
                    data, addr = udp_sock.recvfrom(65536)
                    attrs = deserialize_frame(data)

                    # Inject into local hwsim radio
                    inject_frame(nl_sock, family_id, local_mac, attrs)

                    rx_count += 1
                    if rx_count % 100 == 0:
                        print(f"[<] RX: {rx_count} frames injected")

    except KeyboardInterrupt:
        print(f"\n[*] Stopped. TX={tx_count} RX={rx_count}")
    finally:
        nl_sock.close()
        udp_sock.close()


if __name__ == "__main__":
    main()
