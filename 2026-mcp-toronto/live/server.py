#!/usr/bin/env python3
"""The live viewer. It reads a run directory and never writes into one.

  server.py serve  --state DIR [--port 8765]        foreground, 127.0.0.1 only
  server.py ensure --state DIR [--port 8765]        start in the background if nothing listens
  server.py start  --state DIR --scene S --out DIR --pace MS   announce a run (launcher only)
  server.py finish --state DIR --out DIR --status N --panel FILE --recount TEXT
  server.py open   [--port 8765]                    open the page in the browser
  server.py stop   [--state DIR] [--port 8765]

Without --port, open and stop use the port recorded in the state's server.json
(default state: the demo's local state directory, under `live`), else 8769.
`serve --port 0` picks a free port and prints it.

The stream is one JSON object per event, in the order the files grew:
run_started, then record (a journal record, body included, nothing invented),
then run_finished with the terminal's panel and recount line.
Only the launcher, which can read the 0600 token file, may start or finish a
run or stop the server. A browser can only read.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
POLL = 0.03
STATIC = {"/": ("index.html", "text/html"), "/live.js": ("live.js", "text/javascript"), "/live.css": ("live.css", "text/css")}


class Tail:
    """Complete new lines of one growing file."""

    def __init__(self, path):
        self.path, self.offset, self.rest, self.count = path, 0, b"", 0

    def lines(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size <= self.offset:
            return []
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            data = handle.read(size - self.offset)
        self.offset += len(data)
        parts = (self.rest + data).split(b"\n")
        self.rest = parts.pop()
        return [p for p in parts if p.strip()]


class Stream:
    """The events of one run: tailed files in, events out, in arrival order."""

    def __init__(self, scene, out, pace_ms, cond, replay=False):
        self.scene, self.out, self.pace_ms, self.cond, self.replay = scene, os.path.abspath(out), pace_ms, cond, replay
        self.id = secrets.token_hex(4)
        self.events, self.finished, self.tails = [], False, {}
        self.emit({"type": "run_started", "id": self.id, "scene": scene, "out": self.out, "pace_ms": pace_ms, "replay": replay})

    def emit(self, event):
        self.events.append(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
        self.cond.notify_all()

    def discover(self):
        base = self.out
        for role in "HGAB":
            path = os.path.join(base, role, "journal.ndjson")
            if os.path.exists(path):
                self.tails.setdefault(path, (Tail(path), "journal", role))
        # The summary file is reported by B's own summary_written record; nothing else is tailed.

    def poll(self):
        self.discover()
        for path in sorted(self.tails, key=lambda p: "HGAB".find(self.tails[p][2]) if self.tails[p][1] == "journal" else 9):
            tail, what, name = self.tails[path]
            for raw in tail.lines():
                tail.count += 1
                try:
                    r = json.loads(raw)
                    self.emit({"type": "record", "role": name, "seq": r["seq"], "kind": r["kind"], "durable": r["durable"], "t_ms": r["t_ms"], "body": r["body"]})
                except (ValueError, KeyError, TypeError) as e:
                    # A line the viewer cannot read is reported, never invented or skipped silently.
                    self.emit({"type": "error", "role": name, "line": tail.count, "reason": f"unreadable journal line: {e}"})

    def finish(self, status, panel, recount):
        self.poll()
        self.finished = True
        self.emit({"type": "run_finished", "status": status, "panel": panel, "recount": recount})


class Hub:
    def __init__(self, state, roots):
        self.state, self.roots = state, [os.path.abspath(r) for r in roots]
        self.cond = threading.Condition()
        self.run, self.stopping, self.history = None, False, []
        self.known = set()   # the exact run directories announced by the launcher
        threading.Thread(target=self.watch, daemon=True).start()

    def watch(self):
        while not self.stopping:
            with self.cond:
                if self.run is not None and not self.run.finished:
                    try:
                        self.run.poll()
                    except Exception as e:   # keep watching; say what happened
                        self.run.emit({"type": "error", "reason": f"viewer poll failed: {e}"})
            time.sleep(POLL)

    def start(self, body):
        with self.cond:
            out = os.path.abspath(body["out"])
            if self.run is not None and not self.run.finished and self.run.out != out:
                if not body.get("replace"):
                    return None   # one run at a time; the launcher is told and may replace it
                # The token holder replaces a run whose launcher never reported its end.
                try:
                    self.run.finish("abandoned", [], "")
                except Exception:
                    self.run.finished = True
                self.history.append({"scene": self.run.scene, "out": self.run.out, "status": "abandoned", "recount": ""})
            self.run = Stream(body["scene"], out, int(body.get("pace_ms") or 0), self.cond)
            self.known.add(os.path.realpath(out))
            return self.run.id

    def finish(self, body):
        with self.cond:
            if self.run is None or self.run.out != os.path.abspath(body["out"]) or self.run.finished:
                return False
            self.run.finish(body.get("status"), body.get("panel") or [], body.get("recount") or "")
            self.history.append({"scene": self.run.scene, "out": self.run.out, "status": body.get("status"), "recount": body.get("recount")})
            return True

    def snapshot(self):
        with self.cond:
            if self.run is None:
                return {"run": None, "history": self.history}
            return {"run": {"id": self.run.id, "scene": self.run.scene, "out": self.run.out, "pace_ms": self.run.pace_ms,
                            "finished": self.run.finished, "events": len(self.run.events)}, "history": self.history}

    def replayed(self, directory):
        """A finished run read back from disk: a run directory under the run root, or one the launcher announced."""
        directory = os.path.realpath(directory)
        roots = [os.path.realpath(r) for r in self.roots]
        if not (any(directory.startswith(root + os.sep) for root in roots) or directory in self.known) or not os.path.isdir(directory):
            return None
        if not os.path.exists(os.path.join(directory, "config.json")):
            return None
        cond = threading.Condition()
        scene = ""
        panel, recount = [], ""
        try:
            config = json.load(open(os.path.join(directory, "config.json")))
            scene = config.get("scene", "")
            panel = open(os.path.join(directory, "panel.txt")).read().splitlines()
            recount = open(os.path.join(directory, "recount.txt")).read().splitlines()[0]
        except (OSError, ValueError, IndexError):
            pass
        with cond:
            stream = Stream(scene, directory, 0, cond, replay=True)
            stream.finish(None, panel, recount)
        return stream


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code, kind, data):
        self.send_response(code)
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text/") or kind == "application/json" else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]") if not (self.headers.get("Host") or "").startswith("[") or "]:" in (self.headers.get("Host") or "") else (self.headers.get("Host") or "").strip("[]")
        if self.client_address[0] not in ("127.0.0.1", "::1") or host not in ("127.0.0.1", "localhost", "::1"):
            self.reply(403, "text/plain", b"loopback only\n")
            return False
        return True

    def do_GET(self):
        if not self.local():
            return
        hub = self.server.hub
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path in STATIC:
            name, kind = STATIC[url.path]
            return self.reply(200, kind, open(os.path.join(HERE, name), "rb").read())
        if url.path == "/ping":
            owned = secrets.compare_digest(self.headers.get("X-ZT-Live") or "", self.server.token)
            return self.reply(200, "application/json", json.dumps({"zt_poc_live": True, "owned": owned}).encode())
        if url.path == "/state":
            return self.reply(200, "application/json", json.dumps(hub.snapshot()).encode())
        if url.path == "/replay":
            try:
                stream = hub.replayed(query.get("dir", [""])[0])
            except Exception as e:
                return self.reply(400, "text/plain", f"the run directory could not be read: {e}\n".encode())
            if stream is None:
                return self.reply(404, "text/plain", b"no such run directory under a known root\n")
            return self.reply(200, "application/json", ("[" + ",".join(stream.events) + "]").encode())
        if url.path == "/events":
            return self.events(query.get("from", ["0"])[0])
        self.reply(404, "text/plain", b"not found\n")

    def events(self, start):
        hub = self.server.hub
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        run, n = None, 0
        try:
            while not hub.stopping:
                with hub.cond:
                    if hub.run is not run:
                        run, n = hub.run, (int(start) if start.isdigit() and run is None else 0)
                    if run is None or len(run.events) <= n:
                        hub.cond.wait(timeout=5)
                    chunk = run.events[n:] if run is not None and hub.run is run else []
                    n += len(chunk)
                data = "".join(f"data: {line}\n\n" for line in chunk) or ": waiting\n\n"
                self.wfile.write(data.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        if not self.local():
            return
        hub = self.server.hub
        # Only the launcher, which can read the token file, may announce or end a run or stop the server.
        if self.headers.get("Origin") is not None or not secrets.compare_digest(self.headers.get("X-ZT-Live") or "", self.server.token):
            return self.reply(403, "text/plain", b"forbidden\n")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/start":
            run_id = hub.start(body)
            if run_id is None:
                return self.reply(409, "application/json", b'{"error":"a run is in progress"}')
            return self.reply(200, "application/json", json.dumps({"id": run_id}).encode())
        if self.path == "/finish":
            return self.reply(200, "application/json", json.dumps({"finished": hub.finish(body)}).encode())
        if self.path == "/stop":
            self.reply(200, "application/json", b'{"stopping":true}')
            hub.stopping = True
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self.reply(404, "text/plain", b"not found\n")


def token_path(state):
    return os.path.join(state, "token")


def read_token(state):
    return open(token_path(state)).read().strip()


def post(port, state, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-ZT-Live": read_token(state)}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read() or b"{}")


def listening(port, state=None):
    """A viewer answers on the port; with a state, only that state's own viewer counts (it recognizes the state's token)."""
    try:
        headers = {"X-ZT-Live": read_token(state)} if state is not None else {}
        request = urllib.request.Request(f"http://127.0.0.1:{port}/ping", headers=headers)
        with urllib.request.urlopen(request, timeout=1) as response:
            answer = json.loads(response.read())
        return answer.get("zt_poc_live") is True and (state is None or answer.get("owned") is True)
    except Exception:
        return False


def serve(state, port):
    os.makedirs(state, exist_ok=True)
    fd = os.open(token_path(state), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secrets.token_hex(16))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    print(port, flush=True)
    server.daemon_threads = True
    server.token = read_token(state)
    server.hub = Hub(state, [os.path.join(os.path.dirname(state), "runs")])
    with open(os.path.join(state, "server.json"), "w") as handle:
        json.dump({"pid": os.getpid(), "port": port}, handle)
    try:
        server.serve_forever()
    finally:
        server.hub.stopping = True


def free(port):
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def port_of(state):
    try:
        return int(json.load(open(os.path.join(state, "server.json")))["port"])
    except (OSError, ValueError, KeyError):
        return None


def ensure(state, port):
    if listening(port, state):
        return True
    if not free(port):
        return False   # another viewer or another program holds this port
    log = open(os.path.join(state, "server.log"), "ab") if os.path.isdir(state) else subprocess.DEVNULL
    os.makedirs(state, exist_ok=True)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", "--state", state, "--port", str(port)],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        if listening(port, state):   # ready means ours: the viewer just started answers this state's token
            return True
        time.sleep(0.05)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "ensure", "start", "finish", "open", "stop"])
    parser.add_argument("--state")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--scene")
    parser.add_argument("--out")
    parser.add_argument("--pace", type=int, default=0)
    parser.add_argument("--status", type=int)
    parser.add_argument("--panel")
    parser.add_argument("--recount", default="")
    parser.add_argument("--replace", action="store_true", help="start: replace a run whose launcher never reported its end")
    a = parser.parse_args()
    default_state = os.path.join(os.path.dirname(HERE), ".local", "live")
    if a.port is None:
        a.port = port_of(a.state or default_state) or 8769
    if a.command == "serve":
        serve(a.state, a.port)
    elif a.command == "ensure":
        sys.exit(0 if ensure(a.state, a.port) else 1)
    elif a.command == "start":
        print(post(a.port, a.state, "/start", {"scene": a.scene, "out": a.out, "pace_ms": a.pace, "replace": a.replace})["id"])
    elif a.command == "finish":
        panel = open(a.panel).read().splitlines() if a.panel and os.path.exists(a.panel) else []
        if not a.recount and a.out and os.path.exists(os.path.join(a.out, "recount.txt")):
            a.recount = open(os.path.join(a.out, "recount.txt")).read().strip().splitlines()[0]
        print(json.dumps(post(a.port, a.state, "/finish", {"out": a.out, "status": a.status, "panel": panel, "recount": a.recount})))
    elif a.command == "open":
        url = f"http://127.0.0.1:{a.port}/"
        opener = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
        try:
            subprocess.Popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            print(f"open {url} in a browser")
    elif a.command == "stop":
        if not listening(a.port):
            print("no viewer is listening")
            return
        state = a.state or default_state
        try:
            print(json.dumps(post(a.port, state, "/stop", {})))
        except Exception as e:
            print(f"could not stop the viewer: {e}")


if __name__ == "__main__":
    main()
