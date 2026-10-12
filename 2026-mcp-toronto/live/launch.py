#!/usr/bin/env python3
"""`demo live <scene|all>`: run a scene paced, with the browser page beside the
terminal. While the scene runs, the terminal prints one line per decision
record (links, the plan check, gate decisions, handler entries, refusals,
commits, effect, exits) from the same event stream the page reads; transport
and housekeeping records only move the page's counters or are not shown. Then
the panel and the recount line.

    launch.py DEMO_DIR scene|all
"""
import json
import os
import subprocess
import sys
import threading
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
PORT = 8769   # resolved in main(): ZT_LIVE_PORT, else the first port that is ours or free from 8769
SCENES = ["healthy", "swap", "gist", "substitute"]
QUIET = {"frame_received", "released", "control_consumed", "response_consumed", "role_started", "server_stopped", "link_closed", "sink_write", "sink_sync"}
# `demo talk`: one pass, one fail, and only the decisions on the terminal; the page carries the rest.
TALK = False
TALK_KINDS = {"plan_checked", "business_consumed", "handler_entry", "commit_made", "tests_ran", "summary_written", "refused", "handler_refused", "handler_failed", "effect_knowledge"}   # plan_checked from every process: each one checks the whole plan itself


def stamp():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")


def line_of(e):
    """One terminal line for one event: role, record kind, and what matters in it."""
    if e["type"] == "error":
        return f"  ! {e.get('role') or '?'}  viewer error at journal line {e.get('line', '?')}: {e.get('reason', 'unreadable record')}"
    if e["type"] != "record":
        return None
    b, kind, role = e["body"], e["kind"], e["role"]
    if TALK and kind not in TALK_KINDS:
        return None
    if kind in QUIET and kind != "released":
        return None
    if kind == "released" and b.get("class") != "business":
        return None
    sha = str(b.get("frame_sha256") or "")[:12]
    what = {
        "plan_checked": lambda: f"whole-plan check: {b['verdict']} " + ("" if not b.get("codes") else "(" + "; ".join(b["codes"]) + ")"),
        "link_ready": lambda: f"link {b['link']} ready, tools listed: {', '.join(b.get('tools') or [])}",
        "business_consumed": lambda: f"decision recorded for {b['link']} {b['tool']}  frame {sha}",
        "released": lambda: f"frame {sha} released on {b['link']}",
        "handler_entry": lambda: f"handler entered: {b['tool']}  (frame {sha})",
        "workspace_ready": lambda: f"workspace ready: jsonlite at fixture commit {str(b.get('base_commit'))[:8]}",
        "commit_made": lambda: f"git commit {str(b.get('commit'))[:8]}: {b.get('message')} ({b.get('edits')} edit)",
        "tests_ran": lambda: f"cargo test {b.get('test')} ... {b.get('status')} ({b.get('passed')} passed, {b.get('failed')} failed, {b.get('duration_ms')} ms)",
        "summary_written": lambda: f"summary written to {b.get('destination')}: {b.get('path')}, receipt {str(b.get('receipt_ref'))[:12]}",
        "refused": lambda: "REFUSED " + "; ".join(f"{f['rule_id']} {f['reason']}" for f in b.get("findings", [])),
        "child_terminal": lambda: f"child {b['node']}: {b['terminal']}, effect {b['effect']}",
        "effect_knowledge": lambda: f"effect: {b['state']} ({b['witness']})",
        "child_exit": lambda: f"{b['role']} exited {b['status']}",
    }.get(kind)
    text = what() if what else kind
    mark = "*" if e.get("durable") else " "
    return f"  {mark} {role}  #{e['seq']:<3} {text}"


def follow(port, run_id, done, printed):
    """Print the stream's lines until run_finished (or the run is over)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=60) as response:
            for raw in response:
                if done.is_set() and raw.strip() == b"":
                    continue
                if not raw.startswith(b"data: "):
                    continue
                e = json.loads(raw[6:])
                if e["type"] == "run_started" and e.get("id") not in (None, run_id):
                    continue
                if e["type"] == "run_finished":
                    break
                text = line_of(e)
                if text:
                    print(text, flush=True)
                    printed.append(text)
    except Exception:
        pass
    finally:
        done.set()


OPENED = False


def open_page():
    """Open the page once, before the first scene, so the run is watched as it happens."""
    global OPENED
    if OPENED or os.environ.get("ZT_NO_OPEN"):
        return
    OPENED = True
    subprocess.call([sys.executable, SERVER, "open", "--port", str(PORT)])
    import time
    time.sleep(1.5)   # let the browser attach its event stream


def choose_port(state):
    """ZT_LIVE_PORT, else the recorded port if our viewer answers there, else the first free port from 8769."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("zt_live_server", SERVER)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    if os.environ.get("ZT_LIVE_PORT"):
        return int(os.environ["ZT_LIVE_PORT"])
    recorded = srv.port_of(state)
    if recorded and srv.listening(recorded, state):
        return recorded
    for port in range(8769, 8779):
        if srv.free(port):
            return port
    return 8769


def run_scene(demo, scene, pace):
    global PORT
    state = os.environ.get("ZT_LIVE_STATE") or os.path.join(demo, ".local", "live")
    os.makedirs(os.path.join(demo, ".local", "runs"), exist_ok=True)
    os.makedirs(state, exist_ok=True)
    PORT = choose_port(state)
    live_ok = subprocess.call([sys.executable, SERVER, "ensure", "--state", state, "--port", str(PORT)]) == 0
    if not live_ok:
        print(f"demo live: no viewer of this state answers on port {PORT}; the scene runs, the live view does not, and this command fails", file=sys.stderr)
    else:
        open_page()
    out = os.environ.get("ZT_OUT") or os.path.join(demo, ".local", "runs", f"{scene}-{stamp()}-{os.getpid()}")
    if os.path.exists(out):
        print(f"demo live: output directory exists: {out}", file=sys.stderr)
        return False
    if TALK:
        label = {"healthy": "pass", "swap": "fail at A, one rule", "gist": "fail, the whole plan", "substitute": "fail at A, five rules"}.get(scene, "")
        print(f"---- {scene}   ({label}; page http://127.0.0.1:{PORT}/)")
    else:
        print(f"---- demo live {scene}   (pace {pace} ms; page http://127.0.0.1:{PORT}/)")
    start = [sys.executable, SERVER, "start", "--state", state, "--port", str(PORT), "--scene", scene, "--out", out, "--pace", str(pace)]
    try:
        run_id = subprocess.check_output(start, stderr=subprocess.PIPE).decode().strip()
    except subprocess.CalledProcessError:
        # The viewer still holds a run whose launcher never reported its end (interrupted); replace it and say so.
        print("demo live: the viewer still held an earlier run that never reported its end; replacing it", file=sys.stderr)
        try:
            run_id = subprocess.check_output(start + ["--replace"], stderr=subprocess.PIPE).decode().strip()
        except subprocess.CalledProcessError as error:
            print(f"demo live: the viewer refused this run; the page will not follow it ({error.stderr.decode(errors='replace').strip()[-160:]})", file=sys.stderr)
            run_id = None
            live_ok = False
    done, printed = threading.Event(), []
    thread = threading.Thread(target=follow, args=(PORT, run_id, done, printed), daemon=True)
    thread.start()
    env = dict(os.environ, ZT_PACE_MS=str(pace))
    binary = os.environ.get("ZT_BIN") or os.path.join(demo, ".local", "target", "debug", "zt")
    proc = subprocess.run([binary, "run", "--scene", scene, "--out", out, "--demo", demo], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with open(os.path.join(out, "host-exit.txt"), "w") as handle:
            handle.write(f"{proc.returncode}\n")
    except OSError:
        pass
    recount = subprocess.run([sys.executable, os.path.join(demo, "qual", "recount.py"), out, os.path.join(demo, "expected", "scenes.json"), scene], stdout=subprocess.PIPE, text=True)
    recount_line = next((l for l in recount.stdout.splitlines() if l.startswith("Independent recount")), recount.stdout.strip())
    try:
        with open(os.path.join(out, "recount.txt"), "w") as handle:
            handle.write(recount.stdout)
    except OSError:
        pass
    # The composition graph of this run for Graphviz: graph.dot, and graph.svg when dot is installed. A view, not a verdict.
    graph = subprocess.run([sys.executable, os.path.join(HERE, "graph.py"), out], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    # Announce the end to the page, then let the terminal follower drain.
    try:
        done_reply = subprocess.run([sys.executable, SERVER, "finish", "--state", state, "--port", str(PORT), "--out", out, "--status", str(proc.returncode),
                                     "--panel", os.path.join(out, "panel.txt"), "--recount", recount_line], stdout=subprocess.PIPE, text=True, check=False)
        if '"finished": true' not in done_reply.stdout:
            print("demo live: the viewer did not accept the end of this run (another run replaced it?)", file=sys.stderr)
            live_ok = False
    except OSError:
        pass
    thread.join(timeout=3.0)
    print()
    if TALK:
        # The panel and the graph are on the page and in the run directory; the terminal keeps the verdict.
        if proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)
        print(recount_line)
        return recount.returncode == 0 and proc.returncode == 0 and live_ok
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    print(recount_line)
    if graph.returncode == 0:
        print(f"graph: graph.dot{' and graph.svg' if 'rendered' in graph.stderr else ''} in the run directory")
    return recount.returncode == 0 and proc.returncode == 0 and live_ok


def main():
    global TALK
    demo, what = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "healthy"
    pace = int(os.environ.get("ZT_PACE_MS") or 300)
    first = True
    if what == "talk":
        # The talk: three scenarios, Enter between them. Everything else is the appendix (`demo live all`).
        TALK = True
        talk = (os.environ.get("ZT_TALK") or "healthy,swap,gist").split(",")
        if any(s not in SCENES for s in talk):
            sys.exit(f"demo talk: scenes must be among {' '.join(SCENES)}, not {talk}")
        ok = True
        for i, scene in enumerate(talk):
            if i:
                try:
                    input(f"     Enter for {scene}: ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    sys.exit(0 if ok else 1)
            ok = run_scene(demo, scene, pace) and ok
        sys.exit(0 if ok else 1)
    if what == "all":
        queue = list(SCENES)
        ok = True
        while queue:
            scene = queue.pop(0)
            if first and os.environ.get("ZT_OUT"):
                os.environ["ZT_OUT_BASE"] = os.environ["ZT_OUT"]
            if os.environ.get("ZT_OUT_BASE"):
                os.environ["ZT_OUT"] = os.path.join(os.environ["ZT_OUT_BASE"], scene)
            ok = run_scene(demo, scene, pace) and ok
            first = False
            if queue:
                try:
                    answer = input(f"     Enter for {queue[0]}, a scene name to jump, or q to stop: ").strip()
                except EOFError:
                    answer = "q"
                if answer == "q":
                    break
                if answer in SCENES:
                    queue = [answer] + [s for s in SCENES if SCENES.index(s) > SCENES.index(answer)]
        sys.exit(0 if ok else 1)
    if what not in SCENES:
        sys.exit(f"demo live: unknown scene {what}; scenes: {' '.join(SCENES)} or all")
    ok = run_scene(demo, what, pace)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
