#!/usr/bin/env python3
"""Leak scan for the demo tree.

    leak_scan.py TREE EXCEPTIONS.json     scan; every finding must be a reviewed exception
    leak_scan.py --selftest               show that each category is detected

Inside a git repository it scans what git would publish from the tree (tracked files,
and untracked files that are not ignored); outside one it walks the tree, skipping
.local, target, .git, __pycache__ and directories named by plain "name/" lines in the
tree's own .gitignore. A private watch list of extra regexes, one per line, may be
named by ZT_LEAK_WATCHLIST; it is read at scan time, must be readable, and is never
published. A category is never switched off. A reviewed exception names one category in one file with
a reason; an exception that matches nothing is an error. Prints JSON, exits 1
on any unreviewed finding or unused exception.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

SKIP = {".local", "target", ".git", "__pycache__"}
# Patterns are assembled from pieces so this file does not flag itself.
PATTERNS = {
    "home_path": re.compile("/" + "Users/"),
    "private_key": re.compile("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----"),
    "jwt_shaped": re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "local_state_path": re.compile(r"\." + "local/"),
    "workspace_path": re.compile("/private/" + "tmp/"),
}


def skipped(tree):
    """SKIP plus every directory a plain "name/" line in the tree's .gitignore names."""
    skip = set(SKIP)
    ignore = os.path.join(tree, ".gitignore")
    if os.path.isfile(ignore):
        with open(ignore) as handle:
            for line in handle:
                name = line.strip()[:-1] if line.strip().endswith("/") else ""
                if name and not name.startswith(("#", "!")) and not set("*?[]/") & set(name):
                    skip.add(name)
    return skip


def patterns():
    """The built-in patterns plus, when ZT_LEAK_WATCHLIST names a file, one regex per non-comment line of it (category watchlist:N)."""
    extra = {}
    path = os.environ.get("ZT_LEAK_WATCHLIST")
    if path:
        if not os.path.isfile(path):
            sys.exit("leak scan: the watch list named by ZT_LEAK_WATCHLIST cannot be read: %s" % path)
        with open(path) as handle:
            for n, line in enumerate(l.strip() for l in handle if l.strip() and not l.lstrip().startswith("#")):
                extra["watchlist:%d" % (n + 1)] = re.compile(line)
    return {**PATTERNS, **extra}


def inventory(tree):
    """Inside a git repository, the files git would publish from the tree: tracked, plus untracked but not ignored.
    Outside one, every file under the tree except the skipped directories."""
    try:
        out = subprocess.run(["git", "-C", tree, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        return sorted(p for p in out.decode().split("\0") if p)
    except (OSError, subprocess.CalledProcessError):
        pass
    found, skip = [], skipped(tree)
    for base, dirs, names in os.walk(tree):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for name in sorted(names):
            found.append(os.path.relpath(os.path.join(base, name), tree))
    return found


def scan(tree):
    findings = []
    active = patterns()
    for relative in inventory(tree):
            path = os.path.join(tree, relative)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                text = handle.read().decode("latin-1")
            for category, pattern in active.items():
                hits = len(pattern.findall(text)) + len(pattern.findall(relative))
                if hits:
                    findings.append({"category": category, "path": relative, "hits": hits})
    return findings


def judge(findings, exceptions):
    allowed = {(e["category"], e["path"]) for e in exceptions}
    found = {(f["category"], f["path"]) for f in findings}
    return [list(x) for x in sorted(found - allowed)], [list(x) for x in sorted(allowed - found)]


def selftest():
    samples = {
        "home_path": "/" + "Users/someone/x", "private_key": "-----BEGIN " + "EC PRIVATE KEY-----",
        "jwt_shaped": "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
        "local_state_path": "x/." + "local/y", "workspace_path": "/private/" + "tmp/x",
    }
    bad = 0
    with tempfile.TemporaryDirectory() as tree:
        with open(os.path.join(tree, "clean.txt"), "w") as f:
            f.write("a clean public file\n")
        bad += scan(tree) != []
        for category, sample in samples.items():
            with open(os.path.join(tree, "probe.txt"), "w") as f:
                f.write(sample + "\n")
            ok = category in {x["category"] for x in scan(tree)}
            bad += not ok
            print("%s %s" % ("ok  " if ok else "BAD ", category))
        os.remove(os.path.join(tree, "probe.txt"))
        os.makedirs(os.path.join(tree, ".local"))
        with open(os.path.join(tree, ".local", "x.txt"), "w") as f:
            f.write(samples["home_path"])
        bad += scan(tree) != []
        os.makedirs(os.path.join(tree, "beside"))
        with open(os.path.join(tree, "beside", "x.txt"), "w") as f:
            f.write(samples["home_path"])
        bad += scan(tree) == []
        with open(os.path.join(tree, ".gitignore"), "w") as f:
            f.write("# nested repositories\nbeside/\n*.pyc\n")
        ok = scan(tree) == []
        bad += not ok
        print("%s directory ignored by the tree's .gitignore is skipped" % ("ok  " if ok else "BAD "))
        unreviewed, unused = judge([{"category": "home_path", "path": "a", "hits": 1}],
                                   [{"category": "home_path", "path": "b", "reason": "x"}])
        bad += not (unreviewed and unused)
    print("leak scanner selftest: %s" % ("all categories detected" if not bad else "%d problems" % bad))
    return 1 if bad else 0


def main(argv):
    if argv == ["--selftest"]:
        return selftest()
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[1]) as handle:
        exceptions = json.load(handle)["exceptions"]
    findings = scan(argv[0])
    unreviewed, unused = judge(findings, exceptions)
    print(json.dumps({"findings": findings, "unreviewed": unreviewed, "unused_exceptions": unused}, indent=1))
    return 1 if unreviewed or unused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
