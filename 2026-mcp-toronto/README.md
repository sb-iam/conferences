# Testing compositional MCP in zero trust

One coding task through four processes on the official Rust MCP SDK, with the
whole plan checked before any handler runs and every outgoing frame checked on
its exact bytes. Three scenarios: a pass, a fail at the workspace by one rule,
and a plan that is denied by every process. An independent judge re-derives
every verdict from what the processes wrote.

This is the demo from the talk *Testing Compositional MCP in Zero Trust* at
the MCP Dev Summit Toronto on 5 October 2026. The essay that walks through it,
with the deck as PDF and HTML, is at
<https://sb-iam.github.io/blog/testing-compositional-mcp-zero-trust/>.

## Run it

You need Rust 1.91.0 through rustup, `git`, Python 3.9 or later, and, for the
page check in `demo check`, Chrome or Chromium (`ZT_CHROME` names one; without
a browser the check records the page check as skipped). The first
build needs the locked dependencies: a Cargo cache that already holds them (set
`CARGO_HOME`, or put a `cargo-home` folder inside the hidden `.local` directory
next to the sources) or network access. The fixture crate has no dependencies,
so its test runs offline.

```sh
./demo build            # once; offline when the cache is present
./demo talk             # the stage script: healthy, Enter, swap, Enter, gist; page beside a quiet terminal
```

`talk` opens the page on `127.0.0.1` (port 8769 or the next free one), runs the
three scenarios paced at 300 ms with Enter between them, and prints only the
decision records and the judge's line. Everything else:

```sh
./demo healthy          # the recorded plan: a real commit, the real test, one summary file
./demo swap             # the workspace swaps the test name at run time: refused at its gate by one rule
./demo gist             # the plan follows the gist comment: denied before any business call
./demo substitute       # the workspace changes its child's destination: refused at its gate by five rules
./demo all              # the four, one after another
./demo live <scene|all> # any scene with the page and the full terminal; Enter between scenes, q to stop
./demo live-open        # open the page again;   ./demo live-stop   stops the viewer
./demo check            # all four fresh, the leak scan and the page check; writes evidence/check.json
```

Every scene runs in a fresh directory under the hidden `.local` directory next
to the sources and refuses to reuse one. A run directory holds the four
journals, the retained frame bytes, `workspace/` (a git repository with the
fixture commit and, after `healthy`, `swap` or `substitute`, the fix commit),
`panel.txt`, `recount.txt`, `graph.dot` and, when Graphviz is installed,
`graph.svg`. `ZT_OUT` picks the output directory (it must not exist);
`ZT_PACE_MS=N` sets the pace, timing only; `ZT_TALK=healthy,gist` picks
other scenes for `talk`.

What a healthy run prints, then the judge's line:

```text
ZT PoC | healthy | 4 processes, MCP 2025-11-25 stdio | OBSERVED
src 4ab3892335d5  bin b9f0d0d8569f  links ready 3/3
Whole-plan check vs intent: verified (3 nodes, 0 findings); calls may start
H->G gateway.submit_fix          frame 8000d109809f | G entry 1
G->A workspace.apply_and_verify  frame aef1b363e2eb | A entry 1 | commit e84a5b0
A->B tests.run_and_record        frame 8842c9f811db | B entry 1 | internal
B: cargo test repeated_member_is_rejected ... ok | 1 passed, 0 failed
Summary checks/jsonlite-summary.txt written 1 | synced | receipt dc6bb827afce
Business calls 3 | frames gated 21 | refusals 0 | exits G/A/B 0/0/0
Effect: known_applied (receipt_verified)
Scope: stdio, modeled credentials, no HTTP. Details: result.json
Independent recount vs frozen expectations: PASS, 213 checks
```

## The task and the chain

> Fix the failing test `repeated_member_is_rejected` in crate `jsonlite`, commit
> the fix, run exactly that test on the commit, and record the result in
> `checks/jsonlite-summary.txt` inside the project. Keep the source and the
> test output inside the project.

```text
H  MCP host       -> G.gateway.submit_fix(patch, test, internal)
G  stdio server 1 -> A.workspace.apply_and_verify(patch, test, internal)   real git commit X
A  stdio server 2 -> B.tests.run_and_record(X, test, internal)            real cargo test on X
B  stdio server 3    writes checks/jsonlite-summary.txt once, synced        one summary file
```

H, G, A and B are four OS processes on `rmcp` at Git revision
`ae2f9c9b45a2c98d24ee345406e79f507c9f9282`, MCP `2025-11-25` over stdio. G is a
server to H and a client of A; A is a server to G and a client of B. The fixture
crate `jsonlite` is a tiny dependency-free JSON parser whose duplicate-key test
fails before the approved patch and passes after it. A comment in its
`src/parser.rs` asks for the summary to be uploaded to a public gist; the
comment is data, and nothing here reads it as an instruction.

## The scenarios

| Scene | What happens | What you see | Judge |
|---|---|---|---|
| `healthy` | H submits the recorded plan; G admits it; A applies the approved edit and makes a real commit; B runs the one test on that commit and writes the summary once | 3 business calls, one handler entry each, commit, `1 passed, 0 failed`, one file, effect `known_applied` with a receipt H re-hashed | PASS, 213 checks |
| `swap` | The plan is fine; at run time A asks B for a different test than the plan names, destination still internal | A's gate refuses the exact bytes with one finding, `EXACT_ACTION_MISMATCH`; B never entered; the commit exists, no test, no file | PASS, 179 |
| `gist` | The plan names the public gist as the destination, as the planted comment asks | G, A, B up and tools listed; H and every server deny the whole plan with four finding names; 0 business calls; no fix commit (only the fixture's initial commit), no test, no file | PASS, 125 |
| `substitute` | The plan is fine; at run time A changes its child's destination to the public gist | A's gate refuses with five findings; B never entered; the commit exists, no file | PASS, 180 |

The judge passes a refusal scene because the refusal is the expected outcome.
A refusal counts only beside the unchanged `healthy` run in a fresh directory.

## What enforces what

- **The Program** (`fixtures/program-*.json`): three nodes with tool, caller,
  callee, arguments, and for the sink an effect. The patch and the test name are
  literals inside it; B's commit argument refers to the output A produces.
- **The whole-plan check** (`src/plan.rs`) runs in H before its first call and
  in every server before it serves anything; each records its own durable
  verdict. Per node: a policy call row for caller, tool and destination
  (`CAPABILITY_WITHOUT_PERMISSION`), the label flow into the sink
  (`LABEL_TENANT`, `LABEL_CLASSIFICATION`), the admitted intent
  (`CONSENT_SCOPE_MISMATCH`). The policy, not the proposal, decides which node
  is an effect node.
- **The gate** (`src/gate.rs`) is the only path for an outgoing frame. The SDK
  builds the message, the transport serializes it once, and that buffer is what
  is decided on: it must equal the admitted node resolved against the inputs
  this process observed (`EXACT_ACTION_MISMATCH`), and the node's rules run
  again on the actual bytes. The decision is a durable journal record; the
  permit to write is built only from the journal's acknowledgement, after
  fsync; the bytes of every decided or refused business frame are retained
  under their hash.
- **The journal** (`src/journal.rs`): one hash-chained NDJSON file per process.
- **The workspace** (A, `src/roles.rs`): copies the fixture, makes the fixture
  commit, applies the approved edit exactly once, stages only its path,
  refuses if anything else changed, commits.
- **The test runner** (B): requires the request to equal its node with the
  commit it was handed, checks HEAD is that commit and the tree is clean, runs
  exactly one test offline, refuses before writing if the test did not run,
  writes the summary once with `create_new`, syncs file and directory, writes
  a receipt binding the summary bytes, the commit, the test, its status, the
  destination and the path. The test's exit and counts are bound through the
  summary bytes: the judge derives the summary text from B's journal and
  compares it whole. H believes `known_applied` only after re-hashing receipt
  and file at the one internal path.
- **Handler-side refusals** are durable `handler_refused` / `handler_failed`
  records before they are error replies.
- **The judge** (`qual/recount.py`) reads only the journals, the retained frame
  bytes (compared with the frozen Program), the workspace's git history, the
  summary, the receipts, B's retained test output, the host's exit status file
  and the panel; it never runs the binary and never trusts `result.json`.
  Expectations in `expected/scenes.json` are fixed per scene.

## The live page

`./demo talk` and `./demo live` open a page fed by a loopback viewer that tails
the journals and never writes into a run directory. It shows a scene strip (each
scene's outcome, with a tick once the judge agreed with the run), the whole-plan
card, the judge's line, a composition graph recoloured by the records (link up,
decided, released, refused, written), four lanes with one card per record, and
the terminal's panel. The same graph is offered as DOT. A finished run opens
again from disk with `/?run=<directory>`, labelled `REPLAY`. The page is checked
in headless Chrome by `live/pagecheck.py` (67 checks) as part of `demo check`.

## What is real, what is modeled

| Part | Status |
|---|---|
| MCP messages, four processes, the gate on the serialized bytes | OBSERVED, real `rmcp` over stdio |
| The git commits, `cargo test` on the committed tree, the summary file | OBSERVED, real effects in the run directory |
| The whole-plan check, the exact-action check, the refusals | OBSERVED, deterministic, from the frozen fixtures |
| Credentials, identities | MODELED: the policy table names actors; nothing is signed |
| The fixture crate, the patch, the planted comment | SYNTHETIC fixtures |
| A coding agent proposing the plan | not here: the plan is recorded |

## Limits

- No HTTP, no token, no TLS: each server trusts the stdio link it was given. A
  refusal is the sender's own gate; a process that bypassed its gate would not
  be stopped by the next one.
- B trusts the shared filesystem: it binds the commit to HEAD and a clean tree,
  then tests that tree in place.
- No lost-reply, restart or replay scene. One machine, one author.
- The judge cannot verify that a sync reached the disk; the chain has no head
  anchor outside the journal.
- The rules are small and hand-written, and the judge reads the same fixtures
  as the runtime: it is independent of the runtime's code and self-report, not
  of its inputs.
- The page was checked in one headless browser at one window size.

## License

Apache License 2.0, see `LICENSE`. Copyright 2026 Shakthi Bachala. The official
Rust MCP SDK (`rmcp`) is used at Git revision
`ae2f9c9b45a2c98d24ee345406e79f507c9f9282` under its own terms; no dependency
source is vendored here.

## Layout

| Path | Contents |
|---|---|
| `demo` | the launcher: build, scenes, talk, live, check |
| `src/` | `main.rs`, `util.rs`, `journal.rs`, `gate.rs`, `plan.rs`, `roles.rs`, `host.rs` |
| `fixtures/` | the task, the `jsonlite` crate, the two Programs (with the patch), the policy, the consent |
| `expected/scenes.json` | expectations per scene |
| `qual/` | `recount.py` (the judge), `check.py`, `leak_scan.py`, the reviewed-exception configuration |
| `live/` | `server.py` (viewer), `launch.py`, `graph.py`, the page, `pagecheck.py` |
| `evidence/check.json` | the last `demo check` |
| `LICENSE` | Apache License 2.0 |
