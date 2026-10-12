//! H, the driver. It starts G (G starts A, A starts B), checks the recorded
//! plan itself before the first call, submits the one task to G through its
//! gate, learns the effect from a receipt it can re-hash, and reports only
//! what the journals and the summary file show.
use crate::{
    gate::{Gate, GatedTransport},
    journal::Journal,
    plan::{Plan, codes},
    roles::{client_config, payload_of},
    util::{Config, Error, Faults, SUMMARY_PATH, pace_ms, sha256_hex, short, write_new, write_new_json},
};
use rmcp::{RoleClient, ServiceError, ServiceExt, model::{CallToolRequestParams, ClientConfig}, service::RunningService};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::Duration,
};
use tokio::process::Child;

type Client = RunningService<RoleClient, ClientConfig>;

const SOURCES: [&[u8]; 7] = [
    include_bytes!("main.rs"),
    include_bytes!("util.rs"),
    include_bytes!("journal.rs"),
    include_bytes!("gate.rs"),
    include_bytes!("plan.rs"),
    include_bytes!("roles.rs"),
    include_bytes!("host.rs"),
];
const SCOPE: &str = "Scope: stdio, modeled credentials, no HTTP. Details: result.json";

pub fn identities() -> Result<(String, String), Error> {
    Ok((sha256_hex(&SOURCES.concat()), sha256_hex(&fs::read(std::env::current_exe()?)?)))
}

fn spawn(cfg: &Config, out: &Path, role: char) -> Result<Child, Error> {
    let dir = out.join(role.to_string());
    fs::create_dir_all(&dir)?;
    Ok(tokio::process::Command::new(&cfg.exe)
        .args(["serve", "--role", &role.to_string(), "--run-dir"])
        .arg(out)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(fs::File::create(dir.join("stderr.log"))?))
        .kill_on_drop(true)
        .spawn()?)
}

async fn connect(gate: &Arc<Gate>, child: &mut Child, link: &'static str) -> Result<Client, Error> {
    let (stdin, stdout) = (child.stdin.take().ok_or("child stdin")?, child.stdout.take().ok_or("child stdout")?);
    let setup = Duration::from_millis(gate.cfg.setup_ms);
    let client = tokio::time::timeout(setup, client_config().serve(GatedTransport::new(stdout, stdin, gate.clone(), link)))
        .await
        .map_err(|_| format!("{link}: setup deadline passed during initialize"))??;
    let tools = tokio::time::timeout(setup, client.peer().list_tools(None))
        .await
        .map_err(|_| format!("{link}: setup deadline passed during tools/list"))??;
    let names: Vec<String> = tools.tools.iter().map(|t| t.name.to_string()).collect();
    let server = client.peer_info().map(|i| json!(i.as_ref())).unwrap_or(Value::Null);
    if server["protocolVersion"] != json!("2025-11-25") {
        return Err(format!("{link}: negotiated protocol is {}", server["protocolVersion"]).into());
    }
    gate.durable("link_ready", json!({"link": link, "tools": names, "protocol": server["protocolVersion"]}))?;
    Ok(client)
}

async fn call(gate: &Arc<Gate>, client: &Client, node: &str) -> Result<Result<Value, ServiceError>, Error> {
    let action = {
        let inputs = gate.inputs.lock().map_err(|_| "lock")?;
        gate.plan.expected_action(node, &inputs)?
    };
    let request: CallToolRequestParams = serde_json::from_value(action)?;
    let deadline = Duration::from_millis(gate.cfg.call_ms);
    match tokio::time::timeout(deadline, client.peer().call_tool(request)).await {
        Ok(Ok(result)) => Ok(Ok(serde_json::to_value(result)?)),
        Ok(Err(e)) => Ok(Err(e)),
        Err(_) => Err(format!("{node}: call deadline passed").into()),
    }
}

/// What H knows about the one effect, and from which bytes it knows it.
fn effect(gate: &Arc<Gate>, out: &Path, result: Result<Value, ServiceError>) -> Result<(), Error> {
    let expected_test = gate.plan.node("verify-B").and_then(|n| n["arguments"]["test"].as_str()).unwrap_or("").to_owned();
    let (state, witness, detail) = match result {
        Ok(value) => {
            let (payload, is_error) = payload_of(&value)?;
            let effect = &payload["outcome"]["effect"];
            if !is_error && effect["state"] == json!("known_applied") {
                // The gateway relays success; H believes it only after re-hashing the receipt and the summary file it names.
                let receipt_ref = effect["receipt_ref"].as_str().unwrap_or("").trim_start_matches("sha256:").to_owned();
                let well_formed = receipt_ref.len() == 64 && receipt_ref.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase());
                let bytes = if well_formed { fs::read(out.join("B").join("receipts").join(format!("{receipt_ref}.json"))).ok() } else { None };
                match bytes {
                    Some(bytes) if sha256_hex(&bytes) == receipt_ref => match serde_json::from_slice::<Value>(&bytes) {
                        Ok(receipt) => {
                            let b = &receipt["binding"];
                            let path = b["path"].as_str().unwrap_or("");
                            let safe = !path.is_empty() && !path.starts_with('/') && !path.split('/').any(|c| c == "..");
                            let file_sha = if safe { fs::read(out.join(path)).ok().map(|f| sha256_hex(&f)) } else { None };
                            let status_is_result = b["status"] == json!("ok") || b["status"] == json!("FAILED");
                            if b["destination"] == json!("internal") && path == SUMMARY_PATH && status_is_result && b["test"] == json!(expected_test) && file_sha.as_deref() == b["summary_sha256"].as_str() {
                                ("known_applied", "receipt_verified", json!({"receipt_ref": receipt_ref, "path": path, "commit": b["commit"], "status": b["status"]}))
                            } else {
                                ("unknown", "receipt_mismatch", json!({"receipt_ref": receipt_ref}))
                            }
                        }
                        Err(_) => ("unknown", "receipt_mismatch", json!({"receipt_ref": receipt_ref, "reason": "receipt is not JSON"})),
                    },
                    Some(_) => ("unknown", "receipt_mismatch", json!({"receipt_ref": receipt_ref, "reason": "bytes do not hash to the reference"})),
                    None => ("unknown", "receipt_unreadable", json!({"receipt_ref": receipt_ref})),
                }
            } else {
                let terminal = effect["downstream_terminal"].as_str().unwrap_or("error_result").to_owned();
                ("unknown", "gateway_reported_uncertainty", json!({"downstream_terminal": terminal}))
            }
        }
        // A "not dispatched" relayed by G is believed only when it carries a refusal record for the sink node.
        Err(ServiceError::McpError(e))
            if e.data.as_ref().is_some_and(|d| {
                d["effect"] == json!("not_dispatched") && d["refusals"].as_array().is_some_and(|r| r.iter().any(|x| x["node"] == json!("verify-B")))
            }) =>
        {
            let refusals = e.data.as_ref().map(|d| d["refusals"].clone()).unwrap_or(Value::Null);
            ("not_dispatched", "refusal_before_release", json!({"message": e.message, "refusals": refusals}))
        }
        Err(ServiceError::McpError(e)) => ("unknown", "gateway_error_reply", json!({"message": e.message})),
        Err(e) => ("unknown", "transport_lost", json!({"error": e.to_string()})),
    };
    gate.durable("effect_knowledge", json!({"node": "verify-B", "state": state, "witness": witness, "detail": detail}))?;
    Ok(())
}

async fn run_scene(cfg: &Arc<Config>, out: &Path) -> Result<(), Error> {
    let plan = Arc::new(Plan::load(cfg)?);
    let journal = Journal::create(&out.join("H"), "H")?;
    let gate = Gate::new('H', cfg.clone(), journal, plan.clone(), &out.join("H"));
    gate.note("role_started", json!({"role": "H", "pid": std::process::id(), "scene": cfg.scene, "program": plan.id()}));
    let mut g = spawn(cfg, out, 'G')?;
    let g_client = connect(&gate, &mut g, "H-G").await?;

    // The whole plan, before the first business call (ZT-R17). The links are up
    // and the tools are listed: a capability is not a permission (ZT-R07).
    let assessment = plan.check();
    gate.durable(
        "plan_checked",
        json!({"program_id": plan.id(), "nodes": plan.nodes().len(), "verdict": assessment.verdict,
               "findings": assessment.findings.iter().map(|f| f.json()).collect::<Vec<_>>(), "codes": codes(&assessment.findings),
               "intent": plan.intent, "role": "H"}),
    )?;
    if assessment.verdict == "verified" {
        let business = async {
            let result = call(&gate, &g_client, "fix-G").await?;
            effect(&gate, out, result)?;
            Ok::<(), Error>(())
        }
        .await;
        if let Err(e) = business {
            gate.durable("effect_knowledge", json!({"node": "verify-B", "state": "unknown", "witness": "host_error", "detail": e.to_string()}))?;
        }
    } else {
        gate.durable("effect_knowledge", json!({"node": "verify-B", "state": "not_dispatched", "witness": "plan_denied_before_any_call", "detail": {"codes": codes(&assessment.findings)}}))?;
    }
    let _ = g_client.cancel().await;
    let status = tokio::time::timeout(Duration::from_millis(cfg.cleanup_ms), g.wait()).await;
    let code = match status {
        Ok(Ok(status)) => json!(status.code()),
        _ => {
            let _ = g.kill().await;
            json!("killed_after_cleanup_deadline")
        }
    };
    gate.durable("child_exit", json!({"role": "G", "status": code}))?;
    Ok(())
}

pub fn journal(out: &Path, role: &str) -> Vec<Value> {
    fs::read(out.join(role).join("journal.ndjson"))
        .map(|bytes| bytes.split(|b| *b == b'\n').filter(|l| !l.is_empty()).filter_map(|l| serde_json::from_slice(l).ok()).collect())
        .unwrap_or_default()
}
fn kinds<'a>(rows: &'a [Value], kind: &str) -> impl Iterator<Item = &'a Value> + 'a {
    let kind = kind.to_owned();
    rows.iter().filter(move |r| r["kind"] == json!(kind))
}

/// Everything the panel shows, read back from the journals and the summary files.
fn counters(out: &Path) -> Value {
    let j: BTreeMap<&str, Vec<Value>> = ["H", "G", "A", "B"].into_iter().map(|r| (r, journal(out, r))).collect();
    let all: Vec<&Value> = j.values().flatten().collect();
    let per_link = |kind: &str| -> Value {
        let mut m = BTreeMap::new();
        for link in ["H-G", "G-A", "A-B"] {
            let n = all.iter().filter(|r| r["kind"] == json!(kind) && r["body"]["link"] == json!(link) && (kind != "released" || r["body"]["class"] == json!("business"))).count();
            m.insert(link, n);
        }
        json!(m)
    };
    let mut frames: BTreeMap<&str, Value> = BTreeMap::new();
    for r in all.iter().filter(|r| r["kind"] == json!("business_consumed")) {
        if let Some(link) = r["body"]["link"].as_str() {
            frames.insert(match link { "H-G" => "H-G", "G-A" => "G-A", _ => "A-B" }, r["body"]["frame_sha256"].clone());
        }
    }
    let handler = |role: &str| kinds(&j[role], "handler_entry").count();
    let written = |d: &str| kinds(&j["B"], "summary_written").filter(|r| r["body"]["destination"] == json!(d)).count();
    let file = |d: &str| {
        let p = if d == "internal" { out.join(SUMMARY_PATH) } else { out.join("B").join(d).join("jsonlite-summary.txt") };
        p.exists() as u64
    };
    let refusals: Vec<Value> = all.iter().filter(|r| r["kind"] == json!("refused") && r["body"]["class"] == json!("business")).map(|r| (*r).clone()).collect();
    let plan = kinds(&j["H"], "plan_checked").next().cloned().unwrap_or(Value::Null);
    let effect = kinds(&j["H"], "effect_knowledge").last().cloned().unwrap_or(Value::Null);
    let exit = |rows: &[Value], role: &str| kinds(rows, "child_exit").find(|r| r["body"]["role"] == json!(role)).map(|r| r["body"]["status"].clone()).unwrap_or(Value::Null);
    let commit = kinds(&j["A"], "commit_made").next().map(|r| r["body"]["commit"].clone()).unwrap_or(Value::Null);
    let tests = kinds(&j["B"], "tests_ran").next().map(|r| r["body"].clone()).unwrap_or(Value::Null);
    let receipt = kinds(&j["B"], "summary_written").next().map(|r| r["body"]["receipt_ref"].clone()).unwrap_or(Value::Null);
    let summary = kinds(&j["B"], "summary_written").next().map(|r| r["body"].clone()).unwrap_or(Value::Null);
    let synced = summary.is_object()
        && kinds(&j["B"], "sink_sync").any(|r| r["body"]["destination"] == summary["destination"] && r["body"]["file"] == json!(true) && r["body"]["directory"] == json!(true));
    let ab_destination = all.iter().find(|r| r["kind"] == json!("business_consumed") && r["body"]["link"] == json!("A-B")).map(|r| r["body"]["destination"].clone()).unwrap_or(Value::Null);
    let handler_refusals = all.iter().filter(|r| r["kind"] == json!("handler_refused") || r["kind"] == json!("handler_failed")).count();
    json!({
        "ab_destination": ab_destination,
        "summary": summary,
        "summary_synced": synced,
        "handler_refusals": handler_refusals,
        "links_ready": all.iter().filter(|r| r["kind"] == json!("link_ready")).count(),
        "plan": plan["body"],
        "business_consumed": per_link("business_consumed"),
        "business_released": per_link("released"),
        "frames": frames,
        "handler_entry": {"G": handler("G"), "A": handler("A"), "B": handler("B")},
        "commits_made": kinds(&j["A"], "commit_made").count(),
        "commit": commit,
        "tests_ran": tests,
        "summary_written": {"internal": written("internal"), "gist-public": written("gist-public")},
        "summary_files": {"internal": file("internal"), "gist-public": file("gist-public")},
        "receipt_ref": receipt,
        "frames_gated": all.iter().filter(|r| ["control_consumed", "response_consumed", "business_consumed"].contains(&r["kind"].as_str().unwrap_or(""))).count(),
        "refusals": refusals,
        "effect": effect["body"],
        "child_exit": {"G": exit(&j["H"], "G"), "A": exit(&j["G"], "A"), "B": exit(&j["A"], "B")},
        "records": j.iter().map(|(k, v)| (k.to_string(), v.len())).collect::<BTreeMap<_, _>>(),
    })
}

fn panel(cfg: &Config, c: &Value, src: &str, bin: &str) -> Vec<String> {
    let n = |v: &Value| v.as_u64().unwrap_or(0);
    let frame = |link: &str| c["frames"][link].as_str().map(short).unwrap_or_else(|| "not sent".into());
    let exit = |role: &str| match &c["child_exit"][role] { Value::Number(x) => x.to_string(), Value::Null => "?".into(), other => other.to_string() };
    let mut lines = vec![
        format!("ZT PoC | {} | 4 processes, MCP 2025-11-25 stdio | OBSERVED", cfg.scene),
        format!("src {}  bin {}  links ready {}/3", short(src), short(bin), n(&c["links_ready"])),
    ];
    let codes: Vec<String> = c["plan"]["codes"].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_owned)).collect()).unwrap_or_default();
    if c["plan"]["verdict"] == json!("verified") {
        lines.push(format!("Whole-plan check vs intent: verified ({} nodes, 0 findings); calls may start", n(&c["plan"]["nodes"])));
    } else {
        lines.push(format!("Whole-plan check vs intent: DENIED ({} findings); no task process is entered", c["plan"]["findings"].as_array().map(Vec::len).unwrap_or(0)));
        lines.extend(wrap(&codes.join("; "), "  "));
    }
    let entry = |role: &str| n(&c["handler_entry"][role]);
    if c["plan"]["verdict"] == json!("verified") {
        lines.push(format!("H->G gateway.submit_fix          frame {} | G entry {}", frame("H-G"), entry("G")));
        let commit = c["commit"].as_str().map(|s| s.chars().take(7).collect::<String>()).unwrap_or_else(|| "none".into());
        lines.push(format!("G->A workspace.apply_and_verify  frame {} | A entry {} | commit {}", frame("G-A"), entry("A"), commit));
        let refusals = c["refusals"].as_array().cloned().unwrap_or_default();
        if let Some(r) = refusals.iter().find(|r| r["body"]["node"] == json!("verify-B")) {
            lines.push(format!("A->B tests.run_and_record        REFUSED at A's gate | B entry {}", entry("B")));
            let mut codes: Vec<String> = r["body"]["findings"].as_array().map(|a| a.iter().map(|f| format!("{} {}", f["rule_id"].as_str().unwrap_or("?"), f["reason"].as_str().unwrap_or("?"))).collect()).unwrap_or_default();
            codes.sort();
            lines.extend(wrap(&codes.join("; "), "  "));
        } else {
            lines.push(format!("A->B tests.run_and_record        frame {} | B entry {} | {}", frame("A-B"), entry("B"), c["ab_destination"].as_str().unwrap_or("?")));
            if c["tests_ran"].is_object() {
                let t = &c["tests_ran"];
                lines.push(format!("B: cargo test {} ... {} | {} passed, {} failed", t["test"].as_str().unwrap_or("?"), t["status"].as_str().unwrap_or("?"), n(&t["passed"]), n(&t["failed"])));
            }
        }
    } else {
        lines.push("G, A and B were up and listed their tools; nothing was sent to them".to_owned());
    }
    let files = (n(&c["summary_files"]["internal"]), n(&c["summary_files"]["gist-public"]));
    if files.0 + files.1 > 0 {
        let path = c["summary"]["path"].as_str().unwrap_or("?");
        let synced = if c["summary_synced"] == json!(true) { "synced" } else { "NOT SYNCED" };
        lines.push(format!("Summary {} written {} | {} | receipt {}", path.strip_prefix("workspace/").unwrap_or(path), files.0 + files.1, synced, c["receipt_ref"].as_str().map(short).unwrap_or_default()));
    } else {
        lines.push("Summary files internal/gist-public 0/0 | nothing written".to_owned());
    }
    let calls: u64 = c["business_released"].as_object().map(|m| m.values().map(n).sum()).unwrap_or(0);
    lines.push(format!("Business calls {} | frames gated {} | refusals {} | exits G/A/B {}/{}/{}", calls, n(&c["frames_gated"]), c["refusals"].as_array().map(Vec::len).unwrap_or(0), exit("G"), exit("A"), exit("B")));
    lines.push(format!("Effect: {} ({})", c["effect"]["state"].as_str().unwrap_or("?"), c["effect"]["witness"].as_str().unwrap_or("?")));
    lines.push(SCOPE.to_owned());
    lines
}
fn wrap(text: &str, indent: &str) -> Vec<String> {
    let mut lines = vec![indent.to_owned()];
    for word in text.split(' ') {
        let last = lines.last_mut().unwrap();
        if last.len() + word.len() + 1 > 80 && !last.trim().is_empty() {
            lines.push(format!("{indent}{word}"));
        } else if last.trim().is_empty() {
            last.push_str(word);
        } else {
            last.push(' ');
            last.push_str(word);
        }
    }
    lines
}

fn configure(scene: &str, out: &Path, demo: &Path) -> Result<Config, Error> {
    let fixtures = demo.join("fixtures");
    let (program, faults) = match scene {
        "healthy" => ("program-healthy.json", Faults::default()),
        "gist" => ("program-gist.json", Faults::default()),
        "substitute" => ("program-healthy.json", Faults { a_child_destination: Some("gist-public".into()), ..Faults::default() }),
        "swap" => ("program-healthy.json", Faults { a_child_test: Some("some_other_test".into()), ..Faults::default() }),
        other => return Err(format!("unknown scene {other}; scenes: healthy swap gist substitute").into()),
    };
    let file = |name: &str| fixtures.join(name).to_string_lossy().into_owned();
    let target = demo.join(".local").join("jsonlite-target");
    fs::create_dir_all(&target)?;
    Ok(Config {
        schema: "poc.config/2".into(),
        run_id: format!("{}-{}", scene, &sha256_hex(out.to_string_lossy().as_bytes())[..8]),
        scene: scene.into(),
        exe: std::env::current_exe()?.to_string_lossy().into_owned(),
        program_file: file(program),
        policy_file: file("policy.json"),
        consent_file: file("consent.json"),
        intent_file: file("task.txt"),
        fixture_dir: fixtures.join("jsonlite").to_string_lossy().into_owned(),
        cargo: std::env::var("ZT_CARGO").unwrap_or_else(|_| "cargo".into()),
        test_target_dir: target.to_string_lossy().into_owned(),
        faults,
        setup_ms: 10_000,
        call_ms: 120_000,
        cleanup_ms: 5_000,
        pace_ms: Some(pace_ms()?).filter(|p| *p > 0),
    })
}

/// `zt run --scene SCENE --out FRESH_DIR --demo DIR`
pub async fn run(scene: &str, out: &Path, demo: &Path) -> Result<i32, Error> {
    if out.exists() {
        return Err(format!("output directory exists: {}; pass a fresh one", out.display()).into());
    }
    fs::create_dir_all(out)?;
    let out: PathBuf = out.canonicalize()?;
    let cfg = Arc::new(configure(scene, &out, &demo.canonicalize()?)?);
    cfg.store(&out)?;
    let outcome = run_scene(&cfg, &out).await;
    let (src, bin) = identities()?;
    let mut c = counters(&out);
    c["scene"] = json!(cfg.scene);
    c["src_sha256"] = json!(src);
    c["bin_sha256"] = json!(bin);
    c["error"] = json!(outcome.as_ref().err().map(|e| e.to_string()));
    write_new_json(&out.join("result.json"), &c)?;
    let lines = panel(&cfg, &c, &src, &bin);
    write_new(&out.join("panel.txt"), format!("{}\n", lines.join("\n")).as_bytes())?;
    for line in &lines {
        println!("{line}");
    }
    match outcome {
        Ok(()) => Ok(0),
        Err(e) => {
            eprintln!("zt: {e}");
            Ok(1)
        }
    }
}
