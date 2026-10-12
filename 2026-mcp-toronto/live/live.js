// The live page. It consumes the viewer's event stream (one journal record per
// event) and shows it three ways: a scene strip, a composition graph, and four
// lanes of cards. Every mark here comes from a record a process wrote; the page
// invents nothing. Numbers in the strip are counted here and labelled so; the
// verdict is the recount's line, shown verbatim.
'use strict';

const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const h12 = s => String(s || '').replace('sha256:', '').slice(0, 12);
const TOOL = {'gateway.submit_fix': 'submit the approved fix', 'workspace.apply_and_verify': 'apply the patch and verify', 'tests.run_and_record': 'run the test and record'};
const PEER = {'H-G': ['H', 'G'], 'G-A': ['G', 'A'], 'A-B': ['A', 'B']};
const SCENES = ['healthy', 'swap', 'gist', 'substitute'];
const SINKS = {'internal': 'sink-internal', 'gist-public': 'sink-gist'};
// What each scene is meant to show; the strip says this once the judge agreed with the run.
const OUTCOME = {healthy: 'PASS', swap: 'FAIL at A', gist: 'FAIL', substitute: 'FAIL at A'};

const S = {events: [], shown: 0, timer: null, replay: false, counts: null, pace: 0, decisions: {}, cards: {}, synced: {}, scenes: {}, graph: null, scene: ''};

// ---- the scene strip: one entry per scene, filled from the viewer's history and the stream ----
function sceneStrip() {
  const nav = $('scenes');
  nav.innerHTML = SCENES.map(s => {
    const st = S.scenes[s] || {state: 'pending'};
    // done: the judge agreed with the run, so the scene showed what it is for; bad: the judge did not.
    const cls = st.state === 'done' ? (OUTCOME[s] === 'PASS' ? 'pass' : 'fail') : st.state;
    const mark = st.state === 'done' ? '✓' : st.state === 'bad' ? '✗' : st.state === 'running' ? '●' : '○';
    const text = st.state === 'done' ? OUTCOME[s] : st.state === 'bad' ? 'judge FAIL' : st.state === 'running' ? 'running' : st.state === 'ended' ? 'ended, no verdict' : 'not run';
    return `<a class="scene ${esc(cls)}" href="${st.out ? '/?run=' + encodeURIComponent(st.out) : '#'}" title="${esc(st.recount || '')}"><span class="mark">${mark}</span><b>${esc(s)}</b><span class="dim">${esc(text)}</span></a>`;
  }).join('');
}
function sceneState(scene, state, out, recount) {
  if (!SCENES.includes(scene)) return;
  S.scenes[scene] = {state, out, recount};
  sceneStrip();
}
function verdictOf(recount) { return /: PASS/.test(recount || '') ? 'done' : (recount ? 'bad' : 'ended'); }

// ---- the composition graph: nodes and edges change class as records arrive ----
function graphReset() {
  S.graph = {nodes: {H: 'idle', G: 'idle', A: 'idle', B: 'idle'}, subs: {H: 'driver', G: 'gateway', A: 'workspace', B: 'tests'},
             edges: {'H-G': 'idle', 'G-A': 'idle', 'A-B': 'idle', 'B-internal': 'idle', 'B-gist': 'idle'},
             labels: {'H-G': 'gateway.submit_fix', 'G-A': 'workspace.apply_and_verify', 'A-B': 'tests.run_and_record', 'B-internal': 'checks/jsonlite-summary.txt', 'B-gist': 'public gist'},
             sinks: {'sink-internal': 'idle', 'sink-gist': 'idle'}, effect: ''};
  for (const n of 'HGAB') gnode(n, 'idle', S.graph.subs[n]);
  for (const e of Object.keys(S.graph.edges)) gedge(e, 'idle', S.graph.labels[e]);
  for (const k of Object.keys(S.graph.sinks)) gsink(k, 'idle', '');
  $('graph-effect').textContent = '';
  dot();
}
function gnode(id, cls, sub) {
  S.graph.nodes[id] = cls; if (sub != null) S.graph.subs[id] = sub;
  const g = $('n-' + id); if (!g) return;
  g.setAttribute('class', 'node ' + cls);
  g.querySelector('.sub').textContent = S.graph.subs[id];
}
function gedge(id, cls, label) {
  S.graph.edges[id] = cls; if (label != null) S.graph.labels[id] = label;
  const g = $('e-' + id); if (!g) return;
  g.setAttribute('class', 'edge ' + cls);
  // Above the line: the tool or the state; below it: the hash or the count. DOT keeps the whole label.
  const [head, tail] = String(S.graph.labels[id]).split(' · ');
  g.querySelector('.elabel').textContent = head.replace(/^(gateway|workspace|tests)\./, '').replace('checks/jsonlite-summary.txt', 'summary file');
  g.querySelector('.ehash').textContent = tail || '';
}
function gsink(id, cls, sub) {
  S.graph.sinks[id] = cls;
  const g = $(id); if (!g) return;
  g.setAttribute('class', 'sink ' + cls);
  g.querySelector('.sub').textContent = sub || '';
}
// The same state as DOT, for Graphviz. Colours and dashes follow the classes above.
function dot() {
  const g = S.graph; if (!g) return;
  const ncol = {idle: 'gray70', up: 'gray40', verified: '#1F4FA3', denied: '#D7322B', entered: '#17161A', done: '#1F4FA3'};
  const ecol = {idle: 'gray70', up: 'gray55', decided: '#F2B81F', released: '#1F4FA3', refused: '#D7322B', written: '#1F4FA3', named: '#D7322B'};
  const lines = ['digraph composition {', '  rankdir=LR; node [shape=box, fontname="Menlo", fontsize=10]; edge [fontname="Menlo", fontsize=9];',
                 `  label="${esc(S.scene || 'run')}${g.effect ? ' · effect ' + g.effect : ''}"; labelloc=t;`];
  for (const n of 'HGAB') lines.push(`  ${n} [label="${n}\\n${g.subs[n]}", color="${ncol[g.nodes[n]] || 'gray70'}", penwidth=${g.nodes[n] === 'idle' ? 1 : 2}];`);
  lines.push(`  sink_internal [label="checks/jsonlite-summary.txt\\n(internal)", shape=note, color="${g.sinks['sink-internal'] === 'written' ? '#1F4FA3' : 'gray70'}"];`);
  lines.push(`  sink_gist [label="public gist\\n(never permitted)", shape=note, style=dashed, color="${g.sinks['sink-gist'] === 'named' ? '#D7322B' : 'gray70'}"];`);
  const target = {'H-G': ['H', 'G'], 'G-A': ['G', 'A'], 'A-B': ['A', 'B'], 'B-internal': ['B', 'sink_internal'], 'B-gist': ['B', 'sink_gist']};
  for (const e of Object.keys(g.edges)) {
    const [a, b] = target[e], cls = g.edges[e];
    const style = cls === 'idle' ? 'dotted' : cls === 'refused' || cls === 'named' ? 'dashed' : 'solid';
    lines.push(`  ${a} -> ${b} [label="${esc(g.labels[e])}", color="${ecol[cls] || 'gray70'}", style=${style}, penwidth=${cls === 'idle' || cls === 'up' ? 1 : 2}];`);
  }
  lines.push('}');
  $('dot').textContent = lines.join('\n');
}

function reset(meta) {
  S.events = []; S.shown = 0; S.replay = !!meta.replay; S.pace = meta.pace_ms || 0; S.scene = meta.scene || '';
  S.counts = {calls: 0, gated: {H: 0, G: 0, A: 0, B: 0}, refusals: 0, rows: 0}; S.decisions = {}; S.cards = {}; S.synced = {};
  document.querySelector('#outcome .label').textContent = S.replay ? 'Outcome · the recount line as written at run time, not re-run' : 'Outcome · what the terminal printed';
  $('scene').textContent = meta.scene || 'run';
  badge(S.replay ? 'replay' : 'live');
  const plan = $('plan'); plan.className = 'card plan pending';
  $('plan-verdict').textContent = 'pending'; $('plan-codes').innerHTML = '';
  for (const r of 'HGAB') { $('lane-' + r).innerHTML = ''; $('gated-' + r).textContent = '0 frames gated'; }
  $('outcome').classList.add('hidden'); $('panel').textContent = ''; $('recount').textContent = ''; $('recount').className = 'recount mono';
  graphReset();
  if (!S.replay) sceneState(meta.scene, 'running', meta.out, '');
  strip();
}
function badge(kind) {
  const b = $('badge');
  b.className = 'badge ' + kind;
  b.textContent = kind === 'live' ? (S.pace ? `LIVE · PACED ${S.pace} MS` : 'LIVE') : kind === 'done' ? 'DONE' : kind === 'replay' ? 'REPLAY · FROM DISK' : 'WAITING';
}
function strip() {
  const c = S.counts || {calls: 0, gated: {H: 0, G: 0, A: 0, B: 0}, refusals: 0, rows: 0};
  $('n-calls').textContent = c.calls; $('n-gated').textContent = Object.values(c.gated).reduce((a, b) => a + b, 0);
  $('n-refusals').textContent = c.refusals; $('n-rows').textContent = c.rows;
}
function card(role, cls, kind, text, sha) {
  const el = document.createElement('div');
  el.className = 'ev ' + cls;
  el.innerHTML = `<div class="k"><span>${esc(kind)}</span>${sha ? `<span class="sha">${esc(h12(sha))}</span>` : ''}</div><div class="t">${text}</div>`;
  const lane = $('lane-' + role);
  lane.appendChild(el);
  lane.scrollTop = lane.scrollHeight;
  return el;
}
const lock = '<span class="lock" title="durable: synced before anything depended on it"></span>';

// One event -> what the page shows for it. Returns nothing for records that
// only add to a counter (control frames, responses, housekeeping).
function show(e) {
  if (e.type !== 'record') return;
  const b = e.body || {}, r = e.role, k = e.kind;
  switch (k) {
    case 'plan_checked': {
      const plan = $('plan');
      plan.className = 'card plan ' + (b.verdict === 'verified' ? 'verified' : 'denied');
      $('plan-verdict').textContent = b.verdict === 'verified' ? `VERIFIED · ${b.nodes} nodes, ${(b.findings || []).length} findings · calls may start` : `DENIED · ${(b.findings || []).length} findings · no business call is made`;
      $('plan-codes').innerHTML = (b.codes || []).map(c => `<span class="chip red">${esc(c)}</span>`).join('');
      if (b.intent) $('intent').textContent = b.intent;
      gnode(r, b.verdict === 'verified' ? 'verified' : 'denied', `plan ${b.verdict}`);
      if (r === 'H') { card(r, 'durable', 'whole-plan check', `${lock}${b.verdict === 'verified' ? 'verified' : 'DENIED'}: ${esc(b.program_id)}`); }
      else { card(r, 'durable quiet', 'whole-plan check, own copy', `${lock}${esc(b.verdict)} before serving`); }
      dot(); return;
    }
    case 'link_ready': {
      const [, to] = PEER[b.link] || [r, '?'];
      if (S.graph.edges[b.link] === 'idle') gedge(b.link, 'up', null);
      if (S.graph.nodes[to] === 'idle') gnode(to, 'up', null);
      card(r, 'durable quiet', 'link ready', `${lock}${esc(b.link)} · tools listed: ${esc((b.tools || []).join(', '))}`);
      dot(); return;
    }
    case 'control_consumed': case 'response_consumed':
      S.counts.gated[r]++; $('gated-' + r).textContent = `${S.counts.gated[r]} frames gated`; strip(); return;
    case 'business_consumed': {
      S.counts.gated[r]++; $('gated-' + r).textContent = `${S.counts.gated[r]} frames gated`;
      const [from, to] = PEER[b.link] || [r, '?'];
      S.decisions[b.frame_sha256] = `${r} #${e.seq}`;
      gedge(b.link, 'decided', `${b.tool} · ${h12(b.frame_sha256)}`);
      S.cards[b.frame_sha256] = card(r, 'durable out', `decision · ${from} → ${to}`, `${lock}${esc(TOOL[b.tool] || b.tool)}${b.destination ? ` · destination <b>${esc(b.destination)}</b>` : ''}<br><span class="dim">decided on the exact bytes and consumed<span class="rel"></span></span>`, b.frame_sha256);
      strip(); dot(); return;
    }
    case 'released':
      if (b.class === 'business') {
        S.counts.calls++; strip();
        gedge(b.link, 'released', null);
        const c = S.cards[b.frame_sha256]; if (c) { const rel = c.querySelector('.rel'); if (rel) rel.textContent = ', then released'; }
        dot();
      }
      return;
    case 'frame_received':
      if (b.method === 'tools/call') {
        const [from] = PEER[b.link] || ['?'];
        const seen = S.decisions[b.frame_sha256];
        card(r, seen ? 'in' : 'in warn', `received · from ${from}`, seen ? `tools/call · the same bytes as the decision at ${esc(seen)} (hash seen in this stream)` : `tools/call · <b>no matching decision seen in the stream</b>`, b.frame_sha256);
      }
      return;
    case 'handler_entry':
      gnode(r, 'entered', `entered ${String(b.tool || '').replace(/^(gateway|workspace|tests)\./, '')}`);
      card(r, 'durable', 'handler entered', `${lock}${esc(TOOL[b.tool] || b.tool)}`, b.frame_sha256);
      dot(); return;
    case 'handler_refused': case 'handler_failed':
      gnode(r, 'denied', `${k === 'handler_refused' ? 'refused' : 'failed'} ${b.code}`);
      card(r, 'durable refused', k === 'handler_refused' ? 'handler REFUSED' : 'handler FAILED', `${lock}${esc(b.code)}${b.detail ? ' · ' + esc(String(b.detail).slice(0, 120)) : ''}`);
      dot(); return;
    case 'sink_sync':
      S.synced[b.destination] = true;
      return;
    case 'workspace_ready':
      gnode(r, S.graph.nodes[r] === 'idle' ? 'up' : S.graph.nodes[r], `fixture at ${String(b.base_commit || '').slice(0, 8)}`);
      card(r, 'durable quiet', 'workspace ready', `${lock}jsonlite checked in at fixture commit ${esc(String(b.base_commit || '').slice(0, 8))}`);
      dot(); return;
    case 'commit_made':
      gnode(r, 'done', `commit ${String(b.commit || '').slice(0, 8)}`);
      card(r, 'durable commit', 'git commit', `${lock}<b>${esc(String(b.commit || '').slice(0, 8))}</b> · ${esc(b.message)} · ${esc(b.edits)} edit`);
      dot(); return;
    case 'tests_ran':
      gnode(r, b.status === 'ok' ? 'done' : 'denied', `test ${b.status} · ${b.passed}/${b.failed}`);
      card(r, 'durable ' + (b.status === 'ok' ? 'commit' : 'refused'), 'cargo test', `${lock}${esc(b.test)} ... <b>${esc(b.status)}</b> · ${esc(b.passed)} passed, ${esc(b.failed)} failed · ${esc(b.duration_ms)} ms`);
      dot(); return;
    case 'summary_written': {
      S.counts.rows++; strip();
      const sink = SINKS[b.destination] || 'sink-gist';
      gedge(sink === 'sink-internal' ? 'B-internal' : 'B-gist', 'written', `written · receipt ${h12(b.receipt_ref)}`);
      gsink(sink, 'written', `written${S.synced[b.destination] ? ', synced' : ''}`);
      card(r, 'durable commit', 'summary written', `${lock}<b>${esc(b.destination)}</b> · ${esc(b.path)} · ${S.synced[b.destination] ? 'file and directory synced (sync record seen)' : 'no sync record seen'} · receipt ${esc(h12(b.receipt_ref))}`);
      dot(); return;
    }
    case 'refused': {
      S.counts.refusals++; strip();
      const codes = (b.findings || []).map(f => `<span class="chip red">${esc(f.rule_id)} ${esc(f.reason)}</span>`).join('');
      if (b.link && S.graph.edges[b.link] != null) gedge(b.link, 'refused', `REFUSED · ${(b.findings || []).length} findings`);
      // Only the forbidden sink is marked when a refused frame named it; a refused frame for the internal file leaves that sink unwritten, not accused.
      if (b.destination === 'gist-public') { gsink('sink-gist', 'named', 'named by a refusal'); gedge('B-gist', 'named', 'asked for · refused at ' + r); }
      card(r, 'durable refused', `REFUSED before release · ${esc(b.link || '')}`, `${lock}refusal recorded${b.destination ? `; the frame named <b>${esc(b.destination)}</b>` : ''}; no release record for this node (the recount checks that)<div class="chips">${codes}</div>`, b.frame_sha256);
      dot(); return;
    }
    case 'child_terminal':
      card(r, 'quiet', 'child call', `${esc(b.node)}: ${esc(b.terminal)} · effect ${esc(b.effect)}`);
      return;
    case 'effect_knowledge':
      S.graph.effect = `${b.state} (${b.witness})`; $('graph-effect').textContent = ` · effect, as H knows it: ${S.graph.effect}`;
      card(r, 'durable effect', 'effect, as H knows it', `${lock}<b>${esc(b.state)}</b> · ${esc(b.witness)}`);
      dot(); return;
    case 'child_exit':
      card(r, 'quiet', 'child exit', `${esc(b.role)} exited ${esc(b.status)}`);
      return;
    case 'error':
      card(r, 'refused', 'viewer error', esc(b.reason || 'unreadable record') + (b.line ? ` · journal line ${esc(b.line)}` : ''));
      return;
    default:
      return;
  }
}

function finish(e) {
  // At the end, each lane shows its first commit, test or refusal card instead of its newest card.
  for (const r of 'HGAB') {
    const lane = $('lane-' + r), key = lane.querySelector('.ev.commit, .ev.refused');
    if (key) lane.scrollTop = Math.max(0, key.getBoundingClientRect().top - lane.getBoundingClientRect().top + lane.scrollTop - 8);
  }
  $('outcome').classList.remove('hidden');
  $('panel').textContent = (e.panel || []).join('\n');
  const rec = $('recount');
  rec.textContent = e.recount || '';
  rec.className = 'recount mono ' + (/: PASS/.test(e.recount || '') ? 'pass' : (e.recount ? 'fail' : ''));
  badge(S.replay ? 'replay' : 'done');
  if (!S.replay) sceneState(S.scene, e.status === 'abandoned' ? 'ended' : verdictOf(e.recount), (S.scenes[S.scene] || {}).out, e.recount);
}

// Events are shown in order; a replay from disk is paced here (and says so),
// a live run arrives at the runtime's own pace.
function pump() {
  S.timer = null;
  if (S.shown >= S.events.length) return;
  const e = S.events[S.shown++];
  if (e.type === 'run_finished') finish(e); else if (e.type === 'error') show({type: 'record', role: e.role || 'H', kind: 'error', body: e}); else show(e);
  const wait = S.replay ? (e.type === 'record' && ['business_consumed', 'handler_entry', 'commit_made', 'tests_ran', 'summary_written', 'refused', 'plan_checked', 'effect_knowledge'].includes(e.kind) ? 450 : 60) : 0;
  S.timer = setTimeout(pump, wait);
}
function accept(e) {
  if (e.type === 'run_started') { reset(e); return; }
  S.events.push(e);
  if (!S.timer) S.timer = setTimeout(pump, 0);
}

// Earlier scenes of this viewer, from its history: the strip starts filled.
async function history() {
  try {
    const res = await fetch('/state');
    if (!res.ok) return;
    const st = await res.json();
    for (const h of st.history || []) if (SCENES.includes(h.scene)) S.scenes[h.scene] = {state: h.status === 'abandoned' ? 'ended' : verdictOf(h.recount), out: h.out, recount: h.recount};
    if (st.run && !st.run.finished && SCENES.includes(st.run.scene)) S.scenes[st.run.scene] = {state: 'running', out: st.run.out, recount: ''};
  } catch (err) { /* the strip stays as it is */ }
  sceneStrip();
}

async function init() {
  S.graph = null; graphReset(); sceneStrip();
  const q = new URLSearchParams(location.search);
  await history();
  if (q.get('run')) {
    const res = await fetch('/replay?dir=' + encodeURIComponent(q.get('run')));
    if (!res.ok) { $('scene').textContent = 'no such run'; return; }
    for (const e of await res.json()) accept(e);
    return;
  }
  const es = new EventSource('/events');
  es.onmessage = m => accept(JSON.parse(m.data));
  es.onerror = () => { badge('waiting'); };
}
init();
