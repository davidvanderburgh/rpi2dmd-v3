"""Player control: shared state, command server and status.json writer.

PlayerState is the thread-safe rendezvous between the scheduler (which
plays scenes and writes status) and the control server (which mutates
flags on behalf of the web UI). The protocol is newline-delimited JSON,
one request per connection, over AF_UNIX (or TCP 127.0.0.1 when AF_UNIX
is unavailable/forced) — see docs/contracts.md.

Python 3.7 compatible; stdlib only.
"""

import json
import os
import socket
import sys
import threading
import time

from rpi2dmd import __version__

from . import paths


class PlayerState(object):
    """Flags + status snapshot shared between scheduler and control server."""

    def __init__(self, cfg, library=None):
        self._lock = threading.RLock()
        self.cfg = cfg
        self.library = library
        self.started_at = time.time()
        self._stop = False
        self._skip = False
        self._paused = False
        self._reload = False
        self._test = False
        self._marquee = None
        self._play = None
        self.sleep_override = None      # None | "sleep" | "wake"
        self.brightness = 50
        self.brightness_override = None
        self.state = "clock"
        self.now_playing = {"type": "clock", "game": "", "name": "",
                            "started_at": self.started_at, "duration_ms": 0}
        self.counts = {}
        self._dirty = True
        self._last_write = 0.0

    # -- stop --------------------------------------------------------------
    def request_stop(self):
        with self._lock:
            self._stop = True
            self._dirty = True

    @property
    def stop_requested(self):
        return self._stop

    # -- skip --------------------------------------------------------------
    def request_skip(self):
        with self._lock:
            self._skip = True

    def take_skip(self):
        with self._lock:
            v = self._skip
            self._skip = False
            return v

    @property
    def skip_pending(self):
        return self._skip

    # -- pause ---------------------------------------------------------------
    def set_paused(self, value):
        with self._lock:
            self._paused = bool(value)
            self._dirty = True

    @property
    def paused(self):
        return self._paused

    # -- reload / test / marquee / play -------------------------------------
    def request_reload(self):
        with self._lock:
            self._reload = True

    def take_reload(self):
        with self._lock:
            v = self._reload
            self._reload = False
            return v

    def request_test(self):
        with self._lock:
            self._test = True

    def take_test(self):
        with self._lock:
            v = self._test
            self._test = False
            return v

    def queue_marquee(self, text):
        with self._lock:
            self._marquee = text

    def take_marquee(self):
        with self._lock:
            v = self._marquee
            self._marquee = None
            return v

    def queue_play(self, item):
        with self._lock:
            self._play = item

    def take_play(self):
        with self._lock:
            v = self._play
            self._play = None
            return v

    def validate_play(self, kind, item_id):
        if self.library is None:
            return True
        part = str(item_id).split("/", 1)
        if len(part) != 2:
            return False
        if kind == "dmd":
            return self.library.get_dmd(part[0], part[1]) is not None
        if kind == "gif":
            return self.library.gif_path(part[0], part[1]) is not None
        return False

    # -- sleep ---------------------------------------------------------------
    def set_sleep_override(self, value):
        with self._lock:
            self.sleep_override = value
            self._dirty = True

    def clear_sleep_override(self):
        with self._lock:
            self.sleep_override = None

    # -- brightness ------------------------------------------------------------
    def set_brightness(self, percent):
        with self._lock:
            self.brightness = int(percent)
            self._dirty = True

    def set_brightness_override(self, percent):
        with self._lock:
            self.brightness_override = max(0, min(100, int(percent)))

    def clear_brightness_override(self):
        with self._lock:
            self.brightness_override = None

    # -- scene / counts ---------------------------------------------------------
    def set_scene(self, state_name, now_playing=None):
        with self._lock:
            self.state = state_name
            if now_playing is not None:
                self.now_playing = dict(now_playing)
            self._dirty = True

    def set_counts(self, counts):
        with self._lock:
            self.counts = dict(counts)
            self._dirty = True

    # -- status ------------------------------------------------------------
    def status(self):
        now = time.time()
        with self._lock:
            return {
                "state": self.state,
                "now_playing": dict(self.now_playing),
                "brightness": self.brightness,
                "tint": self.cfg.get("display.tint", "amber"),
                "uptime_s": int(now - self.started_at),
                "started_at": self.started_at,
                "counts": dict(self.counts),
                "version": __version__,
                "updated_at": now,
            }

    def tick(self):
        """Write status.json if anything changed or 5s elapsed."""
        self.write_status(force=False)

    def write_status(self, force=True):
        now = time.time()
        with self._lock:
            if not force and not self._dirty and now - self._last_write < 5.0:
                return
            doc = self.status()
            self._dirty = False
            self._last_write = now
        path = paths.status_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, path)
        except OSError:
            pass


class ControlServer(threading.Thread):
    """Line-delimited JSON command server (one request per connection)."""

    def __init__(self, state):
        threading.Thread.__init__(self, name="control")
        self.daemon = True
        self.state = state
        self._stopped = False
        self._unix_path = None

    def stop(self):
        self._stopped = True

    # -- socket ------------------------------------------------------------
    def _bind(self):
        if paths.use_tcp_control():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(paths.control_tcp())
        else:
            path = paths.control_socket_path()
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(path)
            self._unix_path = path
        return sock

    def run(self):
        try:
            sock = self._bind()
        except OSError as e:
            sys.stderr.write("control server bind failed: %s\n" % e)
            return
        sock.listen(5)
        sock.settimeout(0.5)
        while not self._stopped and not self.state.stop_requested:
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception as e:
                sys.stderr.write("control request failed: %s\n" % e)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        try:
            sock.close()
        except OSError:
            pass
        if self._unix_path:
            try:
                os.unlink(self._unix_path)
            except OSError:
                pass

    def _handle(self, conn):
        conn.settimeout(2.0)
        buf = b""
        while b"\n" not in buf and len(buf) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0].strip()
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("not an object")
            resp = self._dispatch(req)
        except ValueError:
            resp = {"ok": False, "error": "bad json"}
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))

    # -- commands ------------------------------------------------------------
    def _dispatch(self, req):
        st = self.state
        cmd = req.get("cmd")
        if cmd == "status":
            doc = {"ok": True}
            doc.update(st.status())
            return doc
        if cmd == "reload_config":
            st.request_reload()
            return {"ok": True}
        if cmd == "pause":
            st.set_paused(True)
            return {"ok": True}
        if cmd == "resume":
            st.set_paused(False)
            return {"ok": True}
        if cmd == "skip":
            st.request_skip()
            return {"ok": True}
        if cmd == "sleep":
            st.set_sleep_override("sleep")
            return {"ok": True}
        if cmd == "wake":
            st.set_sleep_override("wake")
            return {"ok": True}
        if cmd == "play":
            kind = req.get("type")
            item_id = req.get("id", "")
            if kind not in ("dmd", "gif"):
                return {"ok": False, "error": "type must be dmd or gif"}
            if not st.validate_play(kind, item_id):
                return {"ok": False, "error": "unknown item: %s" % item_id}
            st.queue_play({"type": kind, "id": item_id})
            st.request_skip()
            return {"ok": True}
        if cmd == "marquee":
            text = str(req.get("text", ""))
            if not text:
                return {"ok": False, "error": "text required"}
            st.queue_marquee(text)
            return {"ok": True}
        if cmd == "test_pattern":
            st.request_test()
            return {"ok": True}
        if cmd == "brightness":
            try:
                pct = int(req.get("percent"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "percent must be 0-100"}
            st.set_brightness_override(pct)
            return {"ok": True}
        if cmd == "stop":
            st.request_stop()
            return {"ok": True}
        return {"ok": False, "error": "unknown command: %r" % (cmd,)}


def send_command(request, timeout=5.0):
    """Client helper: send one command dict, return the response dict."""
    if paths.use_tcp_control():
        sock = socket.create_connection(paths.control_tcp(), timeout=timeout)
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(paths.control_socket_path())
    try:
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    finally:
        sock.close()
