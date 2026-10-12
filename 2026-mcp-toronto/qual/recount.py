#!/usr/bin/env python3
"""Independent recount of one run directory against the frozen expectations.

It reads only what the processes wrote: the four journals, the retained frame
bytes, the workspace's git history, the summary file, B's receipts, B's retained
test output (B/test-output.txt), the host's exit status as the launcher wrote
it (host-exit.txt), and the panel. It never runs the zt binary and never trusts
the counters in result.json or config.json. It shares no code with the runtime.

    recount.py RUN_DIR expected/scenes.json [SCENE]

SCENE is the scene the launcher asked for; the panel must name the same one.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROLES = "HGAB"
LINKS = ("H-G", "G-A", "A-B")
TOOLS = {"H-G": "gateway.submit_fix", "G-A": "workspace.apply_and_verify", "A-B": "tests.run_and_record"}
SINK_PATHS = {"internal": os.path.join("workspace", "checks", "jsonlite-summary.txt"), "gist-public": os.path.join("B", "gist-public", "jsonlite-summary.txt")}
NODE_OF = {"H-G": "fix-G", "G-A": "fix-A", "A-B": "verify-B"}
# Records that authorize or report a decision must have been synced before anything depended on them.
DURABLE_KINDS = {"plan_checked", "link_ready", "business_consumed", "refused", "handler_entry", "handler_refused", "handler_failed",
                 "workspace_ready", "commit_made", "tests_ran", "summary_written", "effect_knowledge", "child_exit"}
ROW_SHAPE = {"seq": int, "role": str, "kind": str, "durable": bool, "prev": str, "body": dict}
checks = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok), detail))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def journal(run, role):
    """Rows of one journal, with its hash chain, sequence and complete tail verified."""
    path = os.path.join(run, role, "journal.ndjson")
    if not os.path.exists(path):
        check(f"{role} journal exists", False, path)
        return []
    data = open(path, "rb").read()
    check(f"{role} journal ends with LF", data.endswith(b"\n"))
    rows, prev = [], "genesis"
    for index, line in enumerate(data.splitlines(keepends=True), start=1):
        try:
            row = json.loads(line)
        except ValueError:
            check(f"{role} journal record {index} parses", False)
            break
        if not isinstance(row, dict) or not all(isinstance(row.get(k), t) for k, t in ROW_SHAPE.items()) or row["role"] != role:
            check(f"{role} journal record {index} has the record shape", False, str(row)[:80])
            break
        check(f"{role} journal chain at {index}", row.get("prev") == prev and row.get("seq") == index)
        prev = sha(line)
        rows.append(row)
    return rows


def kinds(rows, kind, **where):
    return [r for r in rows if r["kind"] == kind and all(r["body"].get(k) == v for k, v in where.items())]


def frame(run, role, kind, digest):
    """The retained bytes of one gated business frame, re-hashed and parsed."""
    path = os.path.join(run, role, "frames", f"{kind}-{digest}.json")
    if not re.fullmatch(r"[0-9a-f]{64}", digest or "") or not os.path.exists(path):
        return None
    data = open(path, "rb").read()
    if sha(data) != digest or not data.endswith(b"\n"):
        return False
    try:
        return json.loads(data)
    except ValueError:
        return False


def git(work, *args):
    """Read-only git over the workspace the run left behind."""
    try:
        out = subprocess.run(["git", "-C", work, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                             env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "PATH": os.environ.get("PATH", "")})
        return out.stdout.strip() if out.returncode == 0 else None
    except OSError:
        return None


def panel_numbers(panel):
    out = {"hops": {}}
    for line in panel:
        m = re.search(r"links ready (\d+)/3$", line)
        if m:
            out.update(links=int(m[1]))
        m = re.search(r"verified \((\d+) nodes, (\d+) findings\)", line)
        if m:
            out.update(nodes=int(m[1]), findings=int(m[2]))
        m = re.search(r"DENIED \((\d+) findings\)", line)
        if m:
            out.update(findings=int(m[1]))
        m = re.match(r"(H->G|G->A|A->B) (\S+) +frame (not sent|[0-9a-f]{12}) \| (G|A|B) entry (\d+)(?: \| (\S+))?", line)
        if m:
            out["hops"][m[1].replace("->", "-")] = {"tool": m[2], "frame": m[3], "role": m[4], "entry": int(m[5]), "tail": m[6]}
        m = re.match(r"A->B (\S+) +REFUSED at A's gate \| B entry (\d+)", line)
        if m:
            out["hops"]["A-B"] = {"tool": m[1], "refused": True, "role": "B", "entry": int(m[2])}
        m = re.match(r"Summary (\S+) written (\d+) \| (synced|NOT SYNCED) \| receipt ([0-9a-f]{12})$", line)
        if m:
            out.update(summary_path=m[1], files_total=int(m[2]), synced=m[3] == "synced", receipt12=m[4])
        m = re.match(r"Business calls (\d+) \| frames gated (\d+) \| refusals (\d+) \| exits G/A/B (\S+)/(\S+)/(\S+)", line)
        if m:
            out.update(calls=int(m[1]), gated=int(m[2]), refusals=int(m[3]), exits=(m[4], m[5], m[6]))
        m = re.match(r"Summary files internal/gist-public (\d+)/(\d+)", line)
        if m:
            out.update(files=(int(m[1]), int(m[2])), files_total=int(m[1]) + int(m[2]))
        m = re.match(r"Effect: (\S+) \((\S+)\)", line)
        if m:
            out.update(effect=m[1], witness=m[2])
        m = re.match(r"Whole-plan check vs intent: (verified|DENIED)", line)
        if m:
            out.update(plan=m[1].lower())
        m = re.match(r"B: cargo test (\S+) \.\.\. (\S+) \| (\d+) passed, (\d+) failed", line)
        if m:
            out.update(test=(m[1], m[2], int(m[3]), int(m[4])))
        m = re.search(r"\| commit ([0-9a-f]{7})$", line)
        if m:
            out.update(commit7=m[1])
    return out


def main(run, expected_file, requested=None):
    expected_all = json.load(open(expected_file))
    panel_path = os.path.join(run, "panel.txt")
    panel = open(panel_path).read().splitlines() if os.path.exists(panel_path) else []
    check("panel exists", bool(panel))
    head = panel[0] if panel else ""
    scene = next((s for s in expected_all["scenes"] if f"| {s} |" in head), None)
    check("panel names a known scene", scene is not None, head)
    if requested is not None:
        check("panel names the scene the launcher asked for", scene == requested, f"{scene} vs {requested}")
        scene = requested if requested in expected_all["scenes"] else scene
    if scene is None:
        return finish()
    e = expected_all["scenes"][scene]
    j = {role: journal(run, role) for role in ROLES}
    everything = [r for rows in j.values() for r in rows]
    program_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(expected_file))), "fixtures", e["program"])
    try:
        program = json.load(open(program_path))
    except (OSError, ValueError):
        program = {}
    node_args = {n.get("id"): n.get("arguments", {}) for n in program.get("nodes", []) if isinstance(n, dict)}
    check("the frozen Program the expectation names is readable", set(node_args) == set(NODE_OF.values()), program_path)

    def program_arguments(link, commits):
        """The node's literal arguments, with the one reference resolved from A's commit record."""
        args = json.loads(json.dumps(node_args.get(NODE_OF[link], {})))
        if isinstance(args.get("commit"), dict) and "$ref" in args["commit"]:
            args["commit"] = commits[0]["body"].get("commit") if commits else None
        return args

    # --- the scene's expectations -------------------------------------------
    plan = kinds(j["H"], "plan_checked")
    check("one whole-plan decision in H", len(plan) == 1, len(plan))
    if plan:
        check("plan verdict", plan[0]["body"]["verdict"] == e["plan_verdict"], plan[0]["body"]["verdict"])
        check("plan finding codes", sorted(plan[0]["body"]["codes"]) == e["plan_finding_codes"], plan[0]["body"]["codes"])
    own_plans = {}
    for role in "GAB":
        p = kinds(j[role], "plan_checked")
        own_plans[role] = p
        check(f"{role} checked the whole plan itself, durably, with the same verdict", len(p) == 1 and p[0]["durable"] is True and p[0]["body"]["verdict"] == e["plan_verdict"], len(p))
    own_plans["H"] = plan
    links = {l: len(kinds(everything, "link_ready", link=l)) for l in LINKS}
    check("links ready", links == e["links_ready"], links)
    consumed = {l: len(kinds(everything, "business_consumed", link=l)) for l in LINKS}
    check("business frames consumed per link", consumed == e["business_consumed"], consumed)
    released = {l: len([r for r in kinds(everything, "released", link=l) if r["body"].get("class") == "business"]) for l in LINKS}
    check("business frames released per link", released == e["business_released"], released)
    entries = {role: len(kinds(j[role], "handler_entry")) for role in "GAB"}
    check("handler entries per role", entries == e["handler_entry"], entries)
    commits = kinds(j["A"], "commit_made")
    check("commits made by the workspace", len(commits) == e["commits_made"], len(commits))
    tests = kinds(j["B"], "tests_ran")
    te = e["tests_ran"]
    check("test runs recorded by B", len(tests) == te["count"], len(tests))
    if tests and te["count"]:
        t = tests[0]["body"]
        keys = ("test", "status", "passed", "failed") + (("exit",) if "exit" in te else ())
        seen = {k: t.get(k) for k in keys}
        check("the named test ran with the expected result", seen == {k: te[k] for k in keys}, seen)
        check("B recorded that the tree was clean at the commit before the test", t.get("tree_clean") is True)
        kept = os.path.join(os.path.dirname(panel_path), "B", "test-output.txt")
        data = open(kept, "rb").read() if os.path.exists(kept) else None
        check("B retained the test output its record names, byte for byte", data is not None and hashlib.sha256(data).hexdigest() == t.get("output_sha256") and len(data) == t.get("output_bytes"), None if data is None else len(data))
    written = {d: len(kinds(j["B"], "summary_written", destination=d)) for d in SINK_PATHS}
    check("summary records per destination", written == e["summary_written"], written)
    files = {d: int(os.path.exists(os.path.join(run, p))) for d, p in SINK_PATHS.items()}
    check("summary files per destination equal the records", files == written, files)
    refused = []
    for role in ROLES:
        for r in kinds(j[role], "refused"):
            if r["body"].get("class") == "business":
                refused.append({"role": role, "link": r["body"]["link"], "node": r["body"]["node"],
                                "codes": sorted(f"{f['rule_id']} {f['reason']}" for f in r["body"]["findings"])})
    check("business refusals", refused == e["refusals"], refused)
    handler_refused = [r for r in everything if r["kind"] in ("handler_refused", "handler_failed")]
    check("handler-side refusals and failures", len(handler_refused) == e["handler_refusals"], [(r["role"], r["body"].get("code")) for r in handler_refused])
    for r in handler_refused:
        received_ids = {(f["body"].get("link"), json.dumps(f["body"].get("id"), separators=(",", ":"))) for f in kinds(j[r["role"]], "frame_received")}
        check(f"{r['role']} handler refusal #{r['seq']} names a request it received", (r["body"].get("link"), r["body"].get("request_id")) in received_ids)
    knowledge = kinds(j["H"], "effect_knowledge")
    check("H recorded its effect knowledge once", len(knowledge) == 1, len(knowledge))
    if knowledge:
        check("effect knowledge", knowledge[-1]["body"]["state"] == e["effect_knowledge"], knowledge[-1]["body"]["state"])
        check("effect witness", knowledge[-1]["body"]["witness"] == e["effect_witness"], knowledge[-1]["body"]["witness"])
    exits = {}
    for role, src in (("G", "H"), ("A", "G"), ("B", "A")):
        rec = kinds(j[src], "child_exit", role=role)
        exits[role] = rec[0]["body"]["status"] if rec else None
    check("child exit statuses", exits == e["child_exit"], exits)
    host_exit_path = os.path.join(os.path.dirname(panel_path), "host-exit.txt")
    host_exit = open(host_exit_path).read().strip() if os.path.exists(host_exit_path) else None
    check("the host's own exit status", host_exit == str(e["host_exit"]), host_exit)

    # --- the workspace: a real repository with the fixture commit, then the fix ---
    work = os.path.join(run, "workspace")
    ready = kinds(j["A"], "workspace_ready")
    if ready:
        base = ready[0]["body"].get("base_commit", "")
        first = git(work, "rev-list", "--max-parents=0", "HEAD")
        check("the workspace's root commit is the fixture commit A recorded", first == base, first)
    if commits:
        c = commits[0]["body"]
        head_commit = git(work, "rev-parse", "HEAD")
        check("the workspace HEAD is the commit A recorded", head_commit == c.get("commit"), head_commit)
        count = git(work, "rev-list", "--count", "HEAD")
        check("the workspace has exactly the fixture commit and the fix", count == "2", count)
        changed = git(work, "diff", "--name-only", "HEAD~1", "HEAD")
        check("the fix changed src/parser.rs only", changed == "src/parser.rs", changed)
        status = git(work, "status", "--porcelain", "--untracked-files=all")
        beyond = [l for l in (status or "").splitlines() if not (l.startswith("?? ") and l[3:] in ("Cargo.lock", "checks/jsonlite-summary.txt"))]
        check("the workspace tree equals the commit apart from the test's lock file and the summary", status is not None and not beyond, beyond)
    else:
        count = git(work, "rev-list", "--count", "HEAD") if os.path.isdir(work) else None
        check("no fix commit exists when none was recorded", count in (None, "1"), count)

    # --- invariants that hold in every scene ----------------------------------
    for role in ROLES:
        for r in j[role]:
            if r["kind"] in DURABLE_KINDS:
                check(f"{role} {r['kind']} #{r['seq']} is durable", r["durable"] is True)
    for role in ROLES:
        by_sha = {}
        own_plan = own_plans.get(role, [])
        for r in j[role]:
            if r["kind"] == "business_consumed":
                by_sha.setdefault(r["body"]["frame_sha256"], r["seq"])
                parsed = frame(run, role, "released", r["body"]["frame_sha256"])
                link = r["body"]["link"]
                check(f"{role} retained the consumed frame {r['body']['frame_sha256'][:12]} and it hashes to its name", isinstance(parsed, dict))
                if isinstance(parsed, dict):
                    params = parsed.get("params", {})
                    check(f"{role} consumed frame on {link} is a tools/call of the link's tool", parsed.get("method") == "tools/call" and params.get("name") == TOOLS.get(link))
                    check(f"{role} consumed frame on {link} names the destination the journal says", params.get("arguments", {}).get("destination") == r["body"].get("destination"))
                    check(f"{role} consumed frame on {link} carries exactly the frozen Program's arguments for {NODE_OF[link]}", params.get("arguments") == program_arguments(link, commits))
            if r["kind"] == "released" and r["body"].get("class") == "business":
                prior = by_sha.get(r["body"]["frame_sha256"])
                check(f"{role} released business frame {r['body']['frame_sha256'][:12]} was consumed first", prior is not None and prior < r["seq"], prior)
                check(f"{role} released a business frame only after its own durable verified plan decision",
                      bool(own_plan) and own_plan[0]["body"]["verdict"] == "verified" and own_plan[0]["durable"] is True and own_plan[0]["seq"] < r["seq"])
            if r["kind"] == "refused" and r["body"].get("class") == "business":
                node = r["body"]["node"]
                check(f"{role} refused node {node} was never released", not kinds(j[role], "released", node=node))
                parsed = frame(run, role, "refused", r["body"]["frame_sha256"])
                check(f"{role} retained the refused frame and it hashes to its name", isinstance(parsed, dict))
                if isinstance(parsed, dict):
                    check(f"{role} refusal record names the refused frame's destination", r["body"].get("destination") == parsed.get("params", {}).get("arguments", {}).get("destination"), r["body"].get("destination"))
                if isinstance(parsed, dict) and scene == "substitute":
                    check("the refused frame really named the public gist", parsed.get("params", {}).get("arguments", {}).get("destination") == "gist-public")
        received = {(r["body"]["link"], r["body"]["frame_sha256"]) for r in kinds(j[role], "frame_received")}
        for r in kinds(j[role], "handler_entry"):
            check(f"{role} handler entry names a received frame", (r["body"]["link"], r["body"]["frame_sha256"]) in received)
    for link, sender, receiver in (("H-G", "H", "G"), ("G-A", "G", "A"), ("A-B", "A", "B")):
        sent = {r["body"]["frame_sha256"] for r in kinds(j[sender], "business_consumed", link=link)}
        got = {r["body"]["frame_sha256"] for r in kinds(j[receiver], "frame_received", link=link) if r["body"].get("method") == "tools/call"}
        check(f"{link} business bytes seen identically by both ends", sent == got, (len(sent), len(got)))
    # Order inside B: handler entry, then the test run, then the summary; the summary follows a test run.
    b_entries = [r["seq"] for r in kinds(j["B"], "handler_entry")]
    for r in kinds(j["B"], "summary_written"):
        b = r["body"]
        check("B wrote the summary after a handler entry and a test run", any(s < r["seq"] for s in b_entries) and any(t["seq"] < r["seq"] for t in tests))
        path = b.get("path", "")
        safe = path and not path.startswith("/") and ".." not in path.split("/")
        data = open(os.path.join(run, path), "rb").read() if safe and os.path.exists(os.path.join(run, path)) else None
        check("the summary file exists where the record says", data is not None, path)
        check("the summary file is at the destination's path", path == SINK_PATHS.get(b.get("destination")), path)
        ref = str(b.get("receipt_ref", ""))
        check("the receipt reference is a sha256 hex name", bool(re.fullmatch(r"[0-9a-f]{64}", ref)))
        rpath = os.path.join(run, "B", "receipts", ref + ".json")
        rbytes = open(rpath, "rb").read() if re.fullmatch(r"[0-9a-f]{64}", ref) and os.path.exists(rpath) else None
        check("the receipt file exists and hashes to its reference", rbytes is not None and sha(rbytes) == ref)
        if data is not None and rbytes is not None:
            try:
                receipt = json.loads(rbytes)
            except ValueError:
                receipt = {}
            binding = receipt.get("binding", {})
            check("the receipt binds the summary bytes", binding.get("summary_sha256") == sha(data) == b.get("summary_sha256"))
            check("the receipt binds the destination, path, test and commit the records name",
                  binding.get("destination") == b.get("destination") and binding.get("path") == path and tests and binding.get("test") == tests[0]["body"].get("test") and commits and binding.get("commit") == commits[0]["body"].get("commit"))
            check("the receipt binds the test status B recorded, and that status is a result", tests and binding.get("status") == tests[0]["body"].get("status") and binding.get("status") in ("ok", "FAILED"), binding.get("status"))
            text = data.decode("utf-8", "replace")
            # The summary is derived here from the journal alone and compared whole: test, status, counts and exit must all agree.
            tb = tests[0]["body"] if tests else {}
            derived = (f"jsonlite summary\ncommit {commits[0]['body'].get('commit')}\ntest {tb.get('test')}: {tb.get('status')}\n"
                       f"passed {tb.get('passed')} failed {tb.get('failed')}\nexit {tb.get('exit')}\n") if commits and tests else None
            check("the summary bytes are exactly the text derived from the journal's commit and test records (test, status, counts, exit)", derived is not None and text == derived, text[:80])
            if knowledge and knowledge[-1]["body"]["state"] == "known_applied":
                check("H's effect knowledge names this receipt", knowledge[-1]["body"].get("detail", {}).get("receipt_ref") == ref)
    # The panel: shape, label, and its numbers held against the journals.
    check("panel fits 12 lines", len(panel) <= 12, len(panel))
    check("panel fits 80 columns", max((len(l) for l in panel), default=0) <= 80, max((len(l) for l in panel), default=0))
    check("panel carries the evidence label", e["panel_label"] in head)
    p = panel_numbers(panel)
    gated = len([r for r in everything if r["kind"] in ("control_consumed", "response_consumed", "business_consumed")])
    check("panel's business calls equal the released business frames", p.get("calls") == sum(released.values()), p.get("calls"))
    check("panel's frames gated equal the consumed frames", p.get("gated") == gated, (p.get("gated"), gated))
    check("panel's refusals equal the refusal records", p.get("refusals") == len(refused), p.get("refusals"))
    check("panel's exits equal the child exit records", p.get("exits") == tuple(str(exits[r]) if exits[r] is not None else "?" for r in "GAB"), p.get("exits"))
    check("panel's summary files equal the files on disk", p.get("files_total") == files["internal"] + files["gist-public"], p.get("files_total"))
    check("panel's links ready equal the link records", p.get("links") == sum(links.values()), p.get("links"))
    if plan:
        body = plan[0]["body"]
        seen = (p.get("nodes"), p.get("findings")) if body["verdict"] == "verified" else (None, p.get("findings"))
        want = (body.get("nodes"), len(body.get("findings", []))) if body["verdict"] == "verified" else (None, len(body.get("findings", [])))
        check("panel's node and finding counts equal H's plan decision", seen == want, seen)
    for link, sender, receiver in (("H-G", "H", "G"), ("G-A", "G", "A"), ("A-B", "A", "B")):
        hop = p["hops"].get(link)
        decided = [r["body"] for r in kinds(j[sender], "business_consumed", link=link)]
        if plan and plan[0]["body"]["verdict"] == "verified":
            check(f"panel has a line for {link}", hop is not None)
        if hop is None:
            continue
        check(f"panel's {link} entry count equals {receiver}'s handler entries", hop["entry"] == entries[receiver], hop["entry"])
        if hop.get("refused"):
            check(f"panel's {link} refusal is {sender}'s refusal record", any(x["role"] == sender and x["link"] == link for x in refused))
            continue
        check(f"panel's {link} frame hash is {sender}'s decision record", hop["frame"] == (decided[0]["frame_sha256"][:12] if len(decided) == 1 else "not sent"), hop["frame"])
        if link == "A-B":
            check("panel's A->B destination word is A's decision record", decided and hop.get("tail") == decided[0].get("destination"), hop.get("tail"))
    summaries = kinds(j["B"], "summary_written")
    if summaries:
        b = summaries[0]["body"]
        shown = str(b.get("path", "")).split("workspace/", 1)[-1]
        synced = any(r["body"].get("destination") == b.get("destination") and r["body"].get("file") is True and r["body"].get("directory") is True for r in kinds(j["B"], "sink_sync"))
        check("panel's summary path is the path B recorded", p.get("summary_path") == shown, p.get("summary_path"))
        check("panel's 'synced' equals B's sync record", p.get("synced") == synced, p.get("synced"))
        check("panel's receipt is the receipt B recorded", p.get("receipt12") == str(b.get("receipt_ref", ""))[:12], p.get("receipt12"))
    if knowledge:
        check("panel's effect line equals H's effect knowledge", (p.get("effect"), p.get("witness")) == (knowledge[-1]["body"]["state"], knowledge[-1]["body"]["witness"]), (p.get("effect"), p.get("witness")))
    if plan:
        check("panel's plan line equals H's plan decision", p.get("plan") == plan[0]["body"]["verdict"], p.get("plan"))
    if tests and te["count"]:
        t = tests[0]["body"]
        check("panel's test line equals B's test record", p.get("test") == (t.get("test"), t.get("status"), t.get("passed"), t.get("failed")), p.get("test"))
    if commits:
        check("panel's commit equals A's commit record", p.get("commit7") == str(commits[0]["body"].get("commit"))[:7], p.get("commit7"))
    return finish()


def finish():
    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"Independent recount vs frozen expectations: FAIL, {len(failed)} of {len(checks)} checks")
        for name, _, detail in failed:
            print(f"  FAIL {name}: {detail}")
        return 1
    print(f"Independent recount vs frozen expectations: PASS, {len(checks)} checks")
    return 0


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    try:
        code = main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    except Exception as error:   # a judge that crashes still prints a verdict line
        check("recount completed without error", False, repr(error)[:200])
        code = finish()
    sys.exit(code)
