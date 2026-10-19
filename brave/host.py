#!/usr/bin/env python3
"""Native messaging host: bridges the PC Watchdog app (unix socket) and the Brave extension (stdio)."""
import json
import os
import socket
import struct
import sys
import threading

SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "pcwatchdog-brave.sock")
lock = threading.Lock()
waiting = {}  # id -> [Event, response]
counter = [0]


def send_ext(msg):
    data = json.dumps(msg).encode()
    with lock:
        sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
        sys.stdout.buffer.flush()


def read_ext():
    while True:
        raw = sys.stdin.buffer.read(4)
        if len(raw) < 4:
            break
        n = struct.unpack("<I", raw)[0]
        msg = json.loads(sys.stdin.buffer.read(n))
        w = waiting.get(msg.get("id"))
        if w:
            w[1] = msg
            w[0].set()
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    os._exit(0)  # Brave closed


def serve_client(conn):
    with conn, conn.makefile("rwb") as f:
        for line in f:
            try:
                req = json.loads(line)
            except ValueError:
                continue
            with lock:
                counter[0] += 1
                rid = counter[0]
            ev = threading.Event()
            waiting[rid] = [ev, None]
            req["id"] = rid
            send_ext(req)
            ok = ev.wait(float(req.get("timeout", 10)))
            resp = waiting.pop(rid)[1] if ok else {"error": "timeout"}
            resp.pop("id", None)
            f.write(json.dumps(resp).encode() + b"\n")
            f.flush()


def main():
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    srv.bind(SOCK)
    os.umask(old)
    srv.listen(4)
    threading.Thread(target=read_ext, daemon=True).start()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve_client, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
