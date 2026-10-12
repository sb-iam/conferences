#!/usr/bin/env python3
"""The composition graph of one finished run, as DOT, from the journals alone.

    graph.py RUN_DIR            prints the DOT; writes RUN_DIR/graph.dot and, when
                                Graphviz's `dot` is on PATH, RUN_DIR/graph.svg

The launcher calls this after each `demo live` scene. The page draws the same
graph live from the stream; this file is the same state for Graphviz. Nodes and
edges take their colour from records: a link_ready makes an edge grey, a
business_consumed yellow, a released blue, a refused red dashed; a node is blue
once its own plan check verified, black once a handler entered, red if it
refused. Nothing here is a verdict: the recount is.
"""
import json
import os
import shutil
import subprocess
import sys

ROLES = "HGAB"
LINKS = {"H-G": ("H", "G"), "G-A": ("G", "A"), "A-B": ("A", "B")}
LABELS = {"H-G": "gateway.submit_fix", "G-A": "workspace.apply_and_verify", "A-B": "tests.run_and_record"}
BLUE, RED, YELLOW, INK = "#1F4FA3", "#D7322B", "#F2B81F", "#17161A"
NODE_COLOUR = {"idle": "gray70", "up": "gray40", "verified": BLUE, "denied": RED, "entered": INK, "done": BLUE}
EDGE_COLOUR = {"idle": "gray70", "up": "gray55", "decided": YELLOW, "released": BLUE, "refused": RED, "written": BLUE, "named": RED}


def journal(run, role):
    path = os.path.join(run, role, "journal.ndjson")
    if not os.path.exists(path):
        return []
    rows = []
    for line in open(path, "rb").read().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def state(run):
    """Node, edge and sink classes with their labels, in journal order per role."""
    nodes = {r: "idle" for r in ROLES}
    subs = {"H": "driver", "G": "gateway", "A": "workspace", "B": "tests"}
    edges = {l: "idle" for l in LINKS}
    edges.update({"B-internal": "idle", "B-gist": "idle"})
    labels = dict(LABELS, **{"B-internal": "checks/jsonlite-summary.txt", "B-gist": "public gist"})
    sinks = {"internal": "idle", "gist-public": "idle"}
    effect = ""
    scene = ""
    for role in ROLES:
        for r in journal(run, role):
            b, k = r.get("body", {}), r.get("kind")
            if k == "role_started" and role == "H":
                scene = b.get("scene", "")
            elif k == "plan_checked":
                nodes[role] = "verified" if b.get("verdict") == "verified" else "denied"
                subs[role] = f"plan {b.get('verdict')}"
            elif k == "link_ready" and b.get("link") in LINKS:
                to = LINKS[b["link"]][1]
                if edges[b["link"]] == "idle":
                    edges[b["link"]] = "up"
                if nodes[to] == "idle":
                    nodes[to] = "up"
            elif k == "business_consumed" and b.get("link") in LINKS:
                edges[b["link"]] = "decided"
                labels[b["link"]] = f"{b.get('tool')} · {str(b.get('frame_sha256', ''))[:12]}"
            elif k == "released" and b.get("class") == "business" and b.get("link") in LINKS:
                edges[b["link"]] = "released"
            elif k == "handler_entry":
                nodes[role] = "entered"
                subs[role] = "entered " + str(b.get("tool", "")).split(".", 1)[-1]
            elif k in ("handler_refused", "handler_failed"):
                nodes[role] = "denied"
                subs[role] = f"{'refused' if k == 'handler_refused' else 'failed'} {b.get('code')}"
            elif k == "commit_made":
                nodes[role] = "done"
                subs[role] = f"commit {str(b.get('commit', ''))[:8]}"
            elif k == "tests_ran":
                nodes[role] = "done" if b.get("status") == "ok" else "denied"
                subs[role] = f"test {b.get('status')} · {b.get('passed')}/{b.get('failed')}"
            elif k == "summary_written":
                key = "B-internal" if b.get("destination") == "internal" else "B-gist"
                edges[key] = "written"
                labels[key] = f"written · receipt {str(b.get('receipt_ref', ''))[:12]}"
                sinks["internal" if key == "B-internal" else "gist-public"] = "written"
            elif k == "refused" and b.get("class") == "business" and b.get("link") in LINKS:
                edges[b["link"]] = "refused"
                labels[b["link"]] = f"REFUSED · {len(b.get('findings', []))} findings"
                if b.get("destination") == "gist-public":
                    # Only the forbidden sink is marked; a refused frame for the internal file leaves it unwritten, not accused.
                    sinks["gist-public"] = "named"
                    edges["B-gist"] = "named"
                    labels["B-gist"] = f"asked for · refused at {role}"
            elif k == "effect_knowledge":
                effect = f"{b.get('state')} ({b.get('witness')})"
    return {"scene": scene, "nodes": nodes, "subs": subs, "edges": edges, "labels": labels, "sinks": sinks, "effect": effect}


def dot(st):
    q = lambda s: str(s).replace("\\", "\\\\").replace('"', '\\"')
    lines = ["digraph composition {", '  rankdir=LR; node [shape=box, fontname="Menlo", fontsize=10]; edge [fontname="Menlo", fontsize=9];',
             f'  label="{q(st["scene"] or "run")}{" · effect " + q(st["effect"]) if st["effect"] else ""}"; labelloc=t;']
    for n in ROLES:
        cls = st["nodes"][n]
        lines.append(f'  {n} [label="{n}\\n{q(st["subs"][n])}", color="{NODE_COLOUR.get(cls, "gray70")}", penwidth={1 if cls == "idle" else 2}];')
    lines.append(f'  sink_internal [label="checks/jsonlite-summary.txt\\n(internal)", shape=note, color="{BLUE if st["sinks"]["internal"] == "written" else "gray70"}"];')
    lines.append(f'  sink_gist [label="public gist\\n(never permitted)", shape=note, style=dashed, color="{RED if st["sinks"]["gist-public"] == "named" else "gray70"}"];')
    target = {"H-G": ("H", "G"), "G-A": ("G", "A"), "A-B": ("A", "B"), "B-internal": ("B", "sink_internal"), "B-gist": ("B", "sink_gist")}
    for e, (a, b) in target.items():
        cls = st["edges"][e]
        style = "dotted" if cls == "idle" else "dashed" if cls in ("refused", "named") else "solid"
        lines.append(f'  {a} -> {b} [label="{q(st["labels"][e])}", color="{EDGE_COLOUR.get(cls, "gray70")}", style={style}, penwidth={1 if cls in ("idle", "up") else 2}];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def write(run):
    text = dot(state(run))
    with open(os.path.join(run, "graph.dot"), "w") as handle:
        handle.write(text)
    rendered = None
    if shutil.which("dot"):
        try:
            with open(os.path.join(run, "graph.svg"), "wb") as out:
                subprocess.run(["dot", "-Tsvg"], input=text.encode(), stdout=out, stderr=subprocess.DEVNULL, timeout=20, check=True)
            rendered = os.path.join(run, "graph.svg")
        except (OSError, subprocess.SubprocessError):
            rendered = None
    return text, rendered


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    text, rendered = write(sys.argv[1])
    sys.stdout.write(text)
    if rendered:
        print(f"rendered {rendered}", file=sys.stderr)
