"""Find BluOS players on the LAN via LSDP (Lenbrook Service Discovery Protocol, UDP 11430)."""
import random
import socket
import struct
import time

LSDP_PORT = 11430
CLASS_PLAYER = 0x0001
CLASS_PLAYER_SECONDARY = 0x0003  # extra nodes of multi-zone units (CI580)


def _query_packet():
    header = bytes([6]) + b"LSDP" + bytes([1])
    classes = struct.pack(">HH", CLASS_PLAYER, CLASS_PLAYER_SECONDARY)
    msg = bytes([3 + len(classes), ord("Q"), 2]) + classes
    return header + msg


def _parse(data):
    """Yield (ip, port, txt dict) for every player announce record in a packet."""
    if len(data) < 6 or data[1:5] != b"LSDP":
        return
    pos = data[0]
    while pos < len(data):
        mlen, mtype = data[pos], data[pos + 1]
        end = pos + mlen
        if mlen == 0:
            return
        if mtype == ord("A"):
            p = pos + 2
            nid_len = data[p]; p += 1 + nid_len
            addr_len = data[p]; p += 1
            ip = socket.inet_ntoa(data[p:p + addr_len]) if addr_len == 4 else None
            p += addr_len
            count = data[p]; p += 1
            for _ in range(count):
                cls = struct.unpack(">H", data[p:p + 2])[0]; p += 2
                txt_count = data[p]; p += 1
                txt = {}
                for _ in range(txt_count):
                    kl = data[p]; p += 1
                    k = data[p:p + kl].decode(errors="replace"); p += kl
                    vl = data[p]; p += 1
                    v = data[p:p + vl].decode(errors="replace"); p += vl
                    txt[k] = v
                if ip and cls in (CLASS_PLAYER, CLASS_PLAYER_SECONDARY):
                    yield ip, int(txt.get("port", 11000)), txt
        pos = end


def discover(timeout=4.0):
    """Broadcast LSDP queries and collect announces. Returns {"ip:port": {"ip","port","name","model"}}."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", LSDP_PORT))
    except OSError:
        sock.bind(("", 0))  # port busy: still get unicast/broadcast replies to our query
    sock.settimeout(0.25)
    found = {}
    packet = _query_packet()
    send_at = [0, 0.5, 1, 2, 3]
    start = time.monotonic()
    while (elapsed := time.monotonic() - start) < timeout:
        while send_at and elapsed >= send_at[0]:
            send_at.pop(0)
            try:
                sock.sendto(packet, ("255.255.255.255", LSDP_PORT))
            except OSError:
                pass
            time.sleep(random.uniform(0, 0.05))
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        for ip, port, txt in _parse(data):
            found[f"{ip}:{port}"] = {"ip": ip, "port": port,
                                     "name": txt.get("name", ip), "model": txt.get("model", "")}
    sock.close()
    return found


if __name__ == "__main__":
    for key, p in discover().items():
        print(key, p)
