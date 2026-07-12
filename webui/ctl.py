"""Control-socket client for the web UI.

Talks to the player daemon over the newline-delimited JSON protocol
described in docs/contracts.md: one request per connection, 1 second
timeout, AF_UNIX on the Pi or TCP 127.0.0.1 on dev machines (see
rpi2dmd.paths). Every failure mode collapses to
{"ok": False, "error": "player offline"} so the web UI keeps working
when the daemon is down.

Python 3.7 compatible; stdlib only. Import after app.py has put the
player package on sys.path.
"""

import json
import socket

from rpi2dmd import paths

TIMEOUT_S = 1.0


def send(cmd, **args):
    """Send one command to the player -> response dict (never raises)."""
    req = dict(args)
    req["cmd"] = cmd
    try:
        if paths.use_tcp_control():
            host, port = paths.control_tcp()
            sock = socket.create_connection((host, port), timeout=TIMEOUT_S)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(TIMEOUT_S)
            sock.connect(paths.control_socket_path())
    except (OSError, ValueError):
        return {"ok": False, "error": "player offline"}
    try:
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 4 * 1024 * 1024:
                break
        line = buf.split(b"\n", 1)[0]
        if not line:
            return {"ok": False, "error": "player offline"}
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict):
            return {"ok": False, "error": "bad response"}
        return resp
    except (OSError, ValueError):
        return {"ok": False, "error": "player offline"}
    finally:
        try:
            sock.close()
        except OSError:
            pass
