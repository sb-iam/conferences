#!/usr/bin/env python3
"""Check the live page itself, in a real browser, over the real event stream.

    pagecheck.py DEMO_DIR [--out DIR]

It starts a viewer of its own on a free port, opens the page in headless
Chrome, runs each scene through `demo live`, reads the page while the scene
runs and after it ends, and keeps a screenshot per scene. Then it opens one
finished run again from disk (`?run=`) and checks the REPLAY badge. It needs
Chrome or Chromium (ZT_CHROME names the program); without one it says so and
exits 3. Each scene's run directory is kept under `--out`/runs/, which is also
the viewer's run root. This is a rehearsal check on one machine and browser; it
is not part of the frozen recount.
"""
import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BROWSERS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "google-chrome", "chromium", "chromium-browser")
SIZE = (1440, 900)
SCENES = ("healthy", "swap", "gist", "substitute")
PLAN = {"healthy": "verified", "swap": "verified", "gist": "denied", "substitute": "verified"}
READ = """(() => {
  const t = id => { const el = document.getElementById(id); return el ? el.textContent : ''; };
  const q = sel => document.querySelectorAll(sel).length;
  const plan = document.getElementById('plan'), rec = document.getElementById('recount');
  const visible = rec && rec.getBoundingClientRect().top >= 0 && rec.getBoundingClientRect().bottom <= window.innerHeight;
  const laneA = document.getElementById('lane-A'), keyA = laneA ? laneA.querySelector('.ev.commit, .ev.refused') : null;
  const keyA_in = !!(keyA && keyA.getBoundingClientRect().top >= laneA.getBoundingClientRect().top - 1 && keyA.getBoundingClientRect().bottom <= laneA.getBoundingClientRect().bottom + 1);
  return {badge: t('badge'), scene: t('scene'), cards: q('.ev'), plan: plan ? plan.className : '', planText: t('plan-verdict'),
          recount: t('recount'), panel: t('panel'), calls: t('n-calls'), gated: t('n-gated'), refusals: t('n-refusals'), rows: t('n-rows'),
          refusedA: q('#lane-A .ev.refused'), commitB: q('#lane-B .ev.commit'), commitA: q('#lane-A .ev.commit'), warn: q('.ev.warn'), verdictVisible: !!visible, keyVisibleA: keyA_in,
          edgesReleased: q('#dag .edge.released'), edgesRefused: q('#dag .edge.refused'), sinkWritten: q('#dag .sink.written'), sinkNamed: q('#dag .sink.named'),
          nodesDenied: q('#dag .node.denied'), nodesEntered: q('#dag .node.entered, #dag .node.done'),
          scenesPass: q('#scenes .scene.pass, #scenes .scene.fail'), scenesRunning: q('#scenes .scene.running'), dot: (document.getElementById('dot') || {textContent: ''}).textContent,
          handlerB: [...document.querySelectorAll('#lane-B .ev .k')].filter(k => k.textContent.includes('handler')).length};
})()"""


class Tab:
    """One browser tab over the DevTools protocol: a minimal WebSocket client."""

    def __init__(self, url):
        hostport, _, path = url[len("ws://"):].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=120)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            head += self.take(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("the browser refused the DevTools connection")
        self.last = 0

    def take(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("the browser closed the connection")
            data += chunk
        return data

    def send(self, payload, op=1):
        mask, n = os.urandom(4), len(payload)
        size = bytes([128 | n]) if n < 126 else bytes([254]) + struct.pack(">H", n) if n < 65536 else bytes([255]) + struct.pack(">Q", n)
        self.sock.sendall(bytes([128 | op]) + size + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def message(self):
        text = b""
        while True:
            first, second = self.take(2)
            n = second & 127
            n = struct.unpack(">H", self.take(2))[0] if n == 126 else struct.unpack(">Q", self.take(8))[0] if n == 127 else n
            data = self.take(n)
            op = first & 15
            if op == 9:
                self.send(data, 10)
            elif op == 8:
                raise RuntimeError("the browser closed the connection")
            elif op in (0, 1, 2):
                text += data
                if first & 128:
                    return json.loads(text)

    def call(self, method, seconds=60, **params):
        """One DevTools call with an absolute deadline: the reply must arrive within `seconds`, whatever else the browser sends."""
        self.last += 1
        self.send(json.dumps({"id": self.last, "method": method, "params": params}).encode())
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"{method}: no reply within {seconds} s")
            self.sock.settimeout(remaining)
            try:
                reply = self.message()
            except socket.timeout:
                raise RuntimeError(f"{method}: no reply within {seconds} s")
            if reply.get("id") == self.last:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def js(self, expression):
        reply = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if "exceptionDetails" in reply:
            raise RuntimeError("page script failed: " + json.dumps(reply["exceptionDetails"])[:300])
        return reply["result"].get("value")

    def read(self):
        return self.js(READ)

    def shot(self, path):
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"]))

    def until(self, test, seconds):
        deadline = time.monotonic() + seconds
        while True:
            state = self.read()
            if test(state) or time.monotonic() > deadline:
                return state
            time.sleep(0.1)


def end_group(pgid):
    """SIGKILL a scene's process group, then each member ps still lists: killpg can report EPERM on macOS while members remain."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], stdout=subprocess.PIPE, universal_newlines=True).stdout
    for f in (l.split() for l in out.splitlines()):
        if len(f) >= 3 and f[1] == str(pgid) and not f[2].startswith("Z"):
            try:
                os.kill(int(f[0]), signal.SIGKILL)
            except OSError:
                pass


def browser_program():
    for name in (os.environ.get("ZT_CHROME"),) + BROWSERS:
        if name and (os.path.exists(name) or shutil.which(name)):
            return name if os.path.exists(name) else shutil.which(name)
    return None


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    demo = os.path.abspath(args[0])
    out = os.path.abspath(args[args.index("--out") + 1]) if "--out" in args else os.path.join(demo, ".local", "pagecheck", time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    program = browser_program()
    if program is None:
        print("Page check skipped: no Chrome or Chromium found (ZT_CHROME names one)")
        return 3
    os.makedirs(os.path.join(out, "shots"))
    state = os.path.join(out, "viewer")
    os.makedirs(state)
    server = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py"), "serve", "--state", state, "--port", "0"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=open(os.path.join(state, "server.log"), "ab"))
    import select
    if not select.select([server.stdout], [], [], 30)[0]:
        server.kill()
        sys.exit("Page check: the viewer did not start within 30 s")
    port = int(server.stdout.readline())
    children = []
    profile = os.path.join(out, "browser-profile")
    browser, version = None, ""   # both set before the try: a browser that never starts still gets its failure receipt
    checks, rows, run_dirs, notes = [], [], {}, []
    failure = None   # a browser or DevTools failure: reported on its own, never as a scene verdict

    def check(name, ok, seen=""):
        checks.append({"check": name, "ok": bool(ok), "seen": str(seen)[:200]})

    def stop(signum, _frame):
        # A supervisor's SIGTERM ends this check through the cleanup below, which ends the scene process groups it started.
        raise SystemExit(f"Page check: stopped by signal {signum}")

    signal.signal(signal.SIGTERM, stop)

    def navigate(tab, url):
        """Navigate with a 30 s deadline; a tab whose navigation never answers is replaced once, over a fresh DevTools connection."""
        try:
            tab.call("Page.navigate", seconds=30, url=url)
            return tab
        except RuntimeError as error:
            notes.append(f"navigation retried once: {error}")
            tab.sock.close()
            pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{debug}/json/list", timeout=10))
            tab = Tab(next(p for p in pages if p["type"] == "page")["webSocketDebuggerUrl"])
            tab.call("Page.enable")
            tab.call("Emulation.setDeviceMetricsOverride", width=SIZE[0], height=SIZE[1], deviceScaleFactor=1, mobile=False)   # per connection
            tab.call("Page.navigate", seconds=30, url=url)
            return tab

    try:
        browser = subprocess.Popen([program, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
                                    f"--user-data-dir={profile}", "--remote-debugging-port=0", f"--window-size={SIZE[0]},{SIZE[1]}", "about:blank"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        active = os.path.join(profile, "DevToolsActivePort")
        for _ in range(300):
            if os.path.exists(active) and open(active).read().strip():
                break
            time.sleep(0.05)
        debug = int(open(active).read().split()[0])
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{debug}/json/list", timeout=10))
        tab = Tab(next(p for p in pages if p["type"] == "page")["webSocketDebuggerUrl"])
        version = tab.call("Browser.getVersion").get("product", "")
        tab.call("Page.enable")
        tab.call("Emulation.setDeviceMetricsOverride", width=SIZE[0], height=SIZE[1], deviceScaleFactor=1, mobile=False)
        tab = navigate(tab, f"http://127.0.0.1:{port}/")
        first = tab.until(lambda s: s["badge"] == "WAITING", 60)
        check("the page loads and waits for a run", first["badge"] == "WAITING", first["badge"])

        for scene in SCENES:
            env = dict(os.environ, ZT_LIVE_PORT=str(port), ZT_LIVE_STATE=state, ZT_NO_OPEN="1", ZT_PACE_MS="150", ZT_OUT=os.path.join(out, "runs", scene))
            shown, errors = os.path.join(out, scene + ".terminal.txt"), os.path.join(out, scene + ".stderr.txt")
            started = time.monotonic()
            run = subprocess.Popen([os.path.join(demo, "demo"), "live", scene], env=env, stdin=subprocess.DEVNULL, stdout=open(shown, "w"), stderr=open(errors, "w"), start_new_session=True)
            children.append(run)
            samples, pictured = [], False
            deadline = time.monotonic() + 300
            while run.poll() is None:
                if time.monotonic() > deadline:
                    end_group(run.pid)
                    check(f"{scene}: the scene finished within 300 s", False, "killed")
                    break
                s = tab.read()
                samples.append((s["badge"], s["cards"], s["scene"], s["scenesRunning"]))
                if not pictured and s["scene"] == scene and s["cards"] >= 6:
                    tab.shot(os.path.join(out, "shots", scene + "-live.png"))
                    pictured = True
                time.sleep(0.2)
            seconds = time.monotonic() - started
            terminal = open(shown).read().splitlines()
            recount = next((l for l in terminal if l.startswith("Independent recount")), "")
            # The panel is the block the launcher prints after the stream lines: after the last blank line, before the recount line.
            block = terminal[:terminal.index(recount)] if recount in terminal else terminal
            blanks = [i for i, l in enumerate(block) if not l.strip()]
            panel = [l for l in block[(blanks[-1] + 1) if blanks else 0:] if l.strip()]
            run_line = next((l for l in terminal if l.startswith("---- demo live")), "")
            end = tab.until(lambda s: s["scene"] == scene and s["badge"] == "DONE" and s["recount"], 60)
            tab.shot(os.path.join(out, "shots", scene + "-end.png"))
            mine = [s for s in samples if s[2] == scene]
            steps = len({s[1] for s in mine})
            live = sorted({s[0] for s in mine if s[0].startswith("LIVE")})
            stderr_text = open(errors).read()
            check(f"{scene}: the terminal ran the scene as usual", run.returncode == 0 and not stderr_text.strip(), f"exit {run.returncode} {stderr_text[:80]}")
            check(f"{scene}: the page reached the end of the run", end["badge"] == "DONE", end["badge"])
            check(f"{scene}: the verdict on the page is the terminal's recount line", end["recount"] == recount and recount, end["recount"])
            check(f"{scene}: the outcome card is the terminal's panel", panel and all(line in end["panel"] for line in panel), f"{len(panel)} panel lines")
            check(f"{scene}: the plan card says {PLAN[scene]}", PLAN[scene] in end["plan"], end["plan"])
            check(f"{scene}: shown live, a step at a time, while it ran", steps >= 5, f"{steps} distinct card counts in {len(mine)} looks")
            check(f"{scene}: the badge said live and gave the pace", "LIVE · PACED 150 MS" in live, live)
            business = next((l for l in panel if l.startswith("Business calls")), "")
            calls = business.split("Business calls ")[1].split(" ")[0] if "Business calls " in business else "?"
            refusals = business.split("refusals ")[1].split(" ")[0] if "refusals " in business else "?"
            check(f"{scene}: the page's call count equals the panel's", end["calls"] == calls, f"{end['calls']} vs {calls}")
            check(f"{scene}: the page's refusal count equals the panel's", end["refusals"] == refusals, f"{end['refusals']} vs {refusals}")
            check(f"{scene}: every received frame matched a decision seen in the stream", end["warn"] == 0, end["warn"])
            check(f"{scene}: the verdict is visible without scrolling at {SIZE[0]}x{SIZE[1]}", end["verdictVisible"], end["verdictVisible"])
            if scene in ("healthy", "swap", "substitute"):
                check(f"{scene}: A's commit card is inside the visible part of its lane at the end", end["keyVisibleA"], end["keyVisibleA"])
            # The scene strip and the composition graph, drawn from the same stream.
            check(f"{scene}: the scene strip showed this scene running, then marked its outcome with the earlier scenes", any(s[3] == 1 for s in mine) and end["scenesPass"] == SCENES.index(scene) + 1, f"{end['scenesPass']} passed")
            dot = end["dot"]
            check(f"{scene}: the DOT text is the graph's state", dot.startswith("digraph composition") and f'label="{scene}' in dot, dot[:60])
            if scene == "healthy":
                check("healthy: the graph shows three released hops, the summary file written, nothing refused", end["edgesReleased"] == 3 and end["sinkWritten"] == 1 and end["edgesRefused"] == 0 and dot.count("style=dotted") == 1, f"{end['edgesReleased']} {end['sinkWritten']} {end['edgesRefused']}")
            if scene == "gist":
                check("gist: the graph shows every process denying the plan and no hop released", end["nodesDenied"] == 4 and end["edgesReleased"] == 0 and end["nodesEntered"] == 0, f"{end['nodesDenied']} {end['edgesReleased']}")
            if scene == "swap":
                check("swap: the graph shows two released hops, the A to B hop refused, no sink named", end["edgesReleased"] == 2 and end["edgesRefused"] == 1 and end["sinkNamed"] == 0, f"{end['edgesReleased']} {end['edgesRefused']} {end['sinkNamed']}")
                check("swap: A shows the refusal and B was never entered", end["refusedA"] == 1 and end["handlerB"] == 0 and end["commitB"] == 0, f"{end['refusedA']} {end['handlerB']} {end['commitB']}")
            if scene == "substitute":
                check("substitute: the graph shows two released hops, the A to B hop refused, the gist sink named by the refused frame", end["edgesReleased"] == 2 and end["edgesRefused"] == 1 and end["sinkNamed"] == 1 and dot.count("style=dashed") == 3, f"{end['edgesReleased']} {end['edgesRefused']} {end['sinkNamed']}")
            if scene == "healthy":
                check("healthy: A shows the commit and B shows the test and the summary", end["commitA"] == 1 and end["commitB"] == 2 and end["rows"] == "1", f"{end['commitA']} {end['commitB']} {end['rows']}")
            if scene == "substitute":
                check("substitute: A shows the refusal and B was never entered", end["refusedA"] == 1 and end["handlerB"] == 0 and end["commitB"] == 0, f"{end['refusedA']} {end['handlerB']} {end['commitB']}")
            if scene == "gist":
                check("gist: no business call was shown", end["calls"] == "0" and end["cards"] > 0, f"{end['calls']} {end['cards']}")
            served = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=10))
            run_dirs[scene] = served["run"]["out"] if served.get("run") else None
            rows.append(f"{scene:<12}{(live[0] if live else '-')[:21]:<22}{end['cards']:>5} cards  {seconds:>5.1f} s  {end['recount'][:34]}")

        # A finished run opened again from disk.
        if run_dirs.get("healthy"):
            tab = navigate(tab, f"http://127.0.0.1:{port}/?run={urllib.request.quote(run_dirs['healthy'])}")
            again = tab.until(lambda s: s["badge"].startswith("REPLAY") and s["recount"], 30)
            check("a finished run opens again from disk as REPLAY with its recount line", again["badge"].startswith("REPLAY") and ": PASS" in again["recount"], f"{again['badge']} {again['recount'][:40]}")
            check("a fresh page fills the scene strip from the viewer's history", again["scenesPass"] == len(SCENES), again["scenesPass"])
            tab.shot(os.path.join(out, "shots", "healthy-replay.png"))
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/replay?dir=/etc", timeout=5)
                check("the viewer refuses to replay a directory outside its roots", False, "200")
            except urllib.error.HTTPError as e:
                check("the viewer refuses to replay a directory outside its roots", e.code == 404, e.code)
    except Exception as error:   # the check did not complete (browser, DevTools, viewer or its own fault): a receipt, not a verdict
        failure = f"{type(error).__name__}: {error}"
    finally:
        for child in children:
            if child.poll() is None:
                end_group(child.pid)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for process in (browser, server):
            if process is None:
                continue
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(profile, ignore_errors=True)

    failed = [c for c in checks if not c["ok"]]
    summary = ["ZT PoC | page check | the page in a real browser on the live stream | CHECKED",
               f"{'scene':<12}{'badge while running':<22}{'cards':>11}  {'took':>7}  verdict on the page"] + rows + [
               f"{version}, headless, {SIZE[0]}x{SIZE[1]}; a screenshot of each scene in shots/",
               "Scope: this machine and browser. A rehearsal check, not the frozen recount"]
    record = {"schema": "poc.page-check/1", "browser": version, "window": list(SIZE), "scenes": list(SCENES), "passed": not failed and failure is None, "checks": checks,
              "notes": notes, "incomplete": failure,
              "limitation": "One machine, one headless browser, one window size. It reads the page's text and state; it does not judge how the page looks."}
    with open(os.path.join(out, "page-check.json"), "w") as handle:
        json.dump(record, handle, indent=1)
        handle.write("\n")
    for line in summary:
        print(line[:80])
    for c in failed:
        print(f"  FAILED: {c['check']} (seen: {c['seen']})")
    for note in notes:
        print(f"  note: {note[:120]}")
    where = os.path.relpath(out, os.path.join(demo, ".local")) if out.startswith(os.path.join(demo, ".local")) else out
    if failure is not None:
        # The check did not complete: this is not a verdict on any scene, and the exit says so (4).
        print(f"Page check: INCOMPLETE after {len(checks)} checks ({failure[:160]}); screenshots in {where}/shots under the demo's local state directory")
        return 4
    print(f"Page check: {'PASS' if not failed else 'FAIL'}, {len(checks)} checks; screenshots in {where}/shots under the demo's local state directory")
    return 0 if not failed else 1


if __name__ == "__main__":
    import urllib.error
    sys.exit(main())
