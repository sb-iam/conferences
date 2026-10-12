#!/usr/bin/env python3
"""The demo's whole check: what `./demo check` runs.

    check.py DEMO_DIR

Steps: build identity, the four scenes in fresh directories, the leak scan,
the actual platform, the page check (Chrome or Chromium; recorded as skipped without one). Writes evidence/check.json with
paths relative to DEMO_DIR. Exits 0 on PASS, 1 on FAIL.
"""
import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time

SCENES = ("healthy", "swap", "gist", "substitute")
failed = []


def step(name, ok, note=""):
    print("check: %s ... %s%s" % (name, "ok" if ok else "FAIL", (" (" + note + ")") if note else ""))
    if not ok:
        failed.append(name)


def group_members(pgid):
    """Live members of a process group as ps lists them (zombies excluded)."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], stdout=subprocess.PIPE, universal_newlines=True).stdout
    return [int(f[0]) for f in (l.split() for l in out.splitlines()) if len(f) >= 3 and f[1] == str(pgid) and not f[2].startswith("Z")]


def end_group(pgid, sig):
    """Signal the whole group, then each member ps still lists: killpg can report EPERM on macOS while members remain."""
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass
    for pid in group_members(pgid):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def wait_group(pgid, seconds):
    deadline = time.monotonic() + seconds
    while group_members(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return group_members(pgid)


def run(cmd, env=None, seconds=900):
    """Run one step in its own process group with a deadline. On overrun the group is first asked to stop (SIGTERM, so a
    step that supervises process groups of its own, like the page check, ends them), then killed; the step reports exit
    124. The output is read on a thread, so a survivor holding the pipe cannot delay the return beyond a few seconds."""
    import threading
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=env, start_new_session=True)
    chunks = []
    reader = threading.Thread(target=lambda: chunks.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()
    try:
        proc.wait(timeout=seconds)
        reader.join(timeout=10)
        return subprocess.CompletedProcess(cmd, proc.returncode, "".join(chunks), "")
    except subprocess.TimeoutExpired:
        note = f"\n(step exceeded {seconds} s; its process group was asked to stop"
        end_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            note += ", did not stop"
        # The parent's exit does not end its group: whatever ignored the request is killed, member by member if killpg will not.
        remaining = group_members(proc.pid)
        if remaining:
            end_group(proc.pid, signal.SIGKILL)
            left = wait_group(proc.pid, 5)
            note += f", then its {len(remaining)} remaining process(es) were killed" + (f"; {len(left)} survived" if left else "")
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=5)
        return subprocess.CompletedProcess(cmd, 124, "".join(chunks) + note + ")\n", "")


def main(root):
    root = os.path.abspath(root)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    result = {"schema": "poc.check/1", "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    binary = os.environ.get("ZT_BIN") or os.path.join(root, ".local", "target", "debug", "zt")
    if os.path.isfile(binary):
        with open(binary, "rb") as handle:
            result["binary_sha256"] = hashlib.sha256(handle.read()).hexdigest()
        step("build identity", True, result["binary_sha256"][:16])
    else:
        result["binary_sha256"] = None
        step("build identity", False, "no binary: run ./demo build")

    result["scenes"] = {}
    for scene in SCENES:
        out = os.path.join(root, ".local", "check", stamp, scene)
        env = dict(os.environ, ZT_OUT=out)
        # Recorded relative to the demo's local state directory, so the file names no machine path.
        entry = {"exit": None, "checks": None, "run": os.path.join("check", stamp, scene)}
        ok = False
        if result["binary_sha256"]:
            proc = run([os.path.join(root, "demo"), scene], env)
            lines = proc.stdout.strip().splitlines()
            last = lines[-1] if lines else ""
            found = re.search(r"(\d+) checks", last)
            entry["exit"] = proc.returncode
            entry["checks"] = int(found.group(1)) if found else None
            ok = proc.returncode == 0 and last.startswith("Independent recount vs frozen expectations: PASS")
            # The runtime prints its own binary hash on the panel; it must be the executable this check hashed.
            panel = os.path.join(out, "panel.txt")
            seen = re.search(r"\bbin ([0-9a-f]{12})\b", open(panel).read()) if os.path.exists(panel) else None
            entry["runtime_bin"] = seen.group(1) if seen else None
            if entry["runtime_bin"] != result["binary_sha256"][:12]:
                ok = False
                entry["identity"] = "the runtime's own hash differs from the hashed executable"
        result["scenes"][scene] = entry
        step("scene " + scene, ok, "%s checks" % entry["checks"] if ok else "")

    # The previous evidence file is this check's own output; judge the sources, then the new evidence below.
    stale = os.path.join(root, "evidence", "check.json")
    if os.path.exists(stale):
        os.remove(stale)
    exceptions = os.path.join(root, "qual", "reviewed-exceptions.json")
    if not os.path.exists(exceptions):
        with open(exceptions, "w") as handle:
            json.dump({"exceptions": []}, handle)
            handle.write("\n")
    proc = subprocess.run([sys.executable, os.path.join(root, "qual", "leak_scan.py"), root, exceptions],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        report = {"findings": None, "unreviewed": None, "unused_exceptions": None}
    result["leak_scan"] = {"exit": proc.returncode, "findings": report["findings"], "unreviewed": report["unreviewed"]}
    step("leak scan", proc.returncode == 0)

    try:
        rustc = run(["rustc", "+1.91.0", "--version"]).stdout.strip()
    except OSError as error:
        rustc = "error: %s" % error
    result["platform"] = {"platform": platform.platform(), "machine": platform.machine(),
                          "python": sys.version.split()[0], "rustc": rustc}
    step("platform", True, rustc)

    pagecheck = os.path.join(root, "live", "pagecheck.py")
    if os.path.exists(pagecheck):
        proc = run([sys.executable, pagecheck, root], dict(os.environ, ZT_NO_OPEN="1"))
        lines = proc.stdout.strip().splitlines()
        kept = os.path.join(root, ".local", "check", stamp, "pagecheck.txt")
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        with open(kept, "w") as handle:
            handle.write(proc.stdout)
        result["page_check"] = {"exit": proc.returncode, "last_line": lines[-1] if lines else "", "output": os.path.join("check", stamp, "pagecheck.txt")}
        if proc.returncode == 3:
            result["page_check"]["status"] = "skipped: no Chrome or Chromium (ZT_CHROME names one)"
            step("page check", True, "skipped: no Chrome or Chromium")
        elif proc.returncode == 4:
            # The page check did not complete (browser, DevTools or viewer): a failure of the rehearsal, kept apart from any scene verdict.
            result["page_check"]["status"] = "incomplete"
            step("page check", False, "did not complete, not a scene verdict: " + (lines[-1] if lines else "")[:100])
        elif proc.returncode == 124:
            result["page_check"]["status"] = "failed: deadline"
            step("page check", False, "exceeded its deadline")
        else:
            step("page check", proc.returncode == 0)
    else:
        result["page_check"] = {"status": "not_run: no live/pagecheck.py"}
        step("page check", True, "not_run: no live/pagecheck.py")

    result["passed"] = not failed
    os.makedirs(os.path.join(root, "evidence"), exist_ok=True)
    with open(os.path.join(root, "evidence", "check.json"), "w") as handle:
        json.dump(result, handle, indent=1)
        handle.write("\n")
    # The evidence just written must itself pass the scan, or the next run would fail on it.
    again = subprocess.run([sys.executable, os.path.join(root, "qual", "leak_scan.py"), os.path.join(root, "evidence"), exceptions],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        clean = not json.loads(again.stdout)["findings"]
    except (ValueError, KeyError):
        clean = False
    step("evidence/check.json", clean, "" if clean else "the evidence file itself carries a flagged pattern")
    if not clean:
        result["passed"] = False
        with open(os.path.join(root, "evidence", "check.json"), "w") as handle:
            json.dump(result, handle, indent=1)
            handle.write("\n")
    skipped = result.get("page_check", {}).get("status", "").startswith("skipped")
    print(("check: PASS without the page check (no browser)" if skipped else "check: PASS") if not failed else "check: FAIL (%s)" % ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
