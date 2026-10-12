//! The three MCP server roles of the demo. G is the gateway (server to H,
//! client of A), A is the workspace (server to G, client of B; it applies the
//! approved patch with a real git commit), B is the test runner (server to A;
//! it runs the exact `cargo test` on that commit and writes the summary file
//! once). Each runs as its own OS process: `zt serve --role G|A|B --run-dir DIR`.
use crate::{
    gate::{Gate, GatedTransport},
    journal::Journal,
    plan::{Plan, tool_of},
    util::{Config, Error, SUMMARY_PATH, canonical, sha256_hex, write_new},
};
use rmcp::{
    ErrorData, Peer, RoleClient, RoleServer, ServerHandler, ServiceError, ServiceExt,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ClientConfig, JsonObject, ListToolsResult,
        PaginatedRequestParams, ProtocolVersion, ServerCapabilities, ServerConfig, Tool,
    },
    service::RequestContext,
};
use serde_json::{Value, json};
use std::{
    fs::{self, File},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Arc,
    time::Duration,
};

fn refused(message: impl std::fmt::Display) -> ErrorData {
    ErrorData::invalid_request(message.to_string(), None)
}
fn failed(message: impl std::fmt::Display) -> ErrorData {
    ErrorData::internal_error(message.to_string(), None)
}
fn plain(s: &str) -> bool {
    !s.is_empty() && s.len() <= 80 && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}
fn hex_commit(s: &str) -> bool {
    (7..=40).contains(&s.len()) && s.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

/// A tool result in the selected wire revision (2025-11-25).
pub fn tool_result(payload: Value, is_error: bool) -> CallToolResult {
    let mut out = if is_error { CallToolResult::structured_error(payload) } else { CallToolResult::structured(payload) };
    out.result_type = None;
    out
}
pub fn payload_of(result: &Value) -> Result<(Value, bool), String> {
    let payload = result.get("structuredContent").cloned().ok_or("tool result without structuredContent")?;
    Ok((payload, result["isError"] == json!(true)))
}

/// `git` in the workspace, with a fixed identity and no user or system configuration.
pub fn git(work: &Path, args: &[&str]) -> Result<String, String> {
    let out = Command::new("git")
        .args(["-c", "user.name=zt-workspace", "-c", "user.email=zt-workspace@example.invalid", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"])
        .args(args)
        .current_dir(work)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .output()
        .map_err(|e| format!("git: {e}"))?;
    if !out.status.success() {
        return Err(format!("git {:?}: {}", args, String::from_utf8_lossy(&out.stderr).trim()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Copy the fixture crate into the run's workspace and make the base commit.
fn prepare_workspace(fixture: &Path, work: &Path) -> Result<String, String> {
    fn copy(src: &Path, dst: &Path) -> Result<(), String> {
        fs::create_dir_all(dst).map_err(|e| e.to_string())?;
        for entry in fs::read_dir(src).map_err(|e| e.to_string())? {
            let entry = entry.map_err(|e| e.to_string())?;
            let name = entry.file_name();
            if name == "target" || name == ".git" {
                continue;
            }
            let (from, to) = (entry.path(), dst.join(&name));
            if from.is_dir() {
                copy(&from, &to)?;
            } else {
                fs::copy(&from, &to).map_err(|e| e.to_string())?;
            }
        }
        Ok(())
    }
    if work.exists() {
        return Err("workspace already exists".into());
    }
    copy(fixture, work)?;
    git(work, &["init", "-q"])?;
    git(work, &["add", "-A"])?;
    git(work, &["commit", "-q", "-m", "fixture: jsonlite before the fix"])?;
    git(work, &["rev-parse", "HEAD"])
}

/// Apply the approved search/replace edits and commit them.
fn apply_and_commit(work: &Path, patch: &Value) -> Result<(String, usize), String> {
    let edits = patch["edits"].as_array().ok_or("patch.edits must be an array")?;
    if edits.is_empty() || edits.len() > 20 {
        return Err("1..=20 edits required".into());
    }
    let mut paths: Vec<&str> = Vec::new();
    for e in edits {
        let path = e["path"].as_str().unwrap_or("");
        if path.is_empty() || path.starts_with('/') || path.split('/').any(|c| c == ".." || c == ".git") {
            return Err(format!("edit path {path:?} not allowed"));
        }
        let full = work.join(path);
        let text = fs::read_to_string(&full).map_err(|e| format!("{path}: {e}"))?;
        let old = e["old_string"].as_str().unwrap_or("");
        if old.is_empty() || text.matches(old).count() != 1 {
            return Err(format!("{path}: old_string must occur exactly once"));
        }
        fs::write(&full, text.replacen(old, e["new_string"].as_str().unwrap_or(""), 1)).map_err(|e| format!("{path}: {e}"))?;
        if !paths.contains(&path) {
            paths.push(path);
        }
    }
    // Only the approved paths are staged. Anything else modified or untracked in the tree refuses the commit.
    for path in &paths {
        git(work, &["add", "--", path])?;
    }
    let status = git(work, &["status", "--porcelain", "--untracked-files=all"])?;
    for line in status.lines() {
        let staged_edit = line.starts_with("M  ") && paths.contains(&line[3..].trim());
        if !staged_edit {
            return Err(format!("workspace has changes outside the approved edits: {line}"));
        }
    }
    let message = patch["message"].as_str().unwrap_or("approved fix");
    git(work, &["commit", "-q", "-m", message])?;
    Ok((git(work, &["rev-parse", "HEAD"])?, edits.len()))
}

/// Run exactly one named test on the workspace, offline, with a shared target directory.
fn run_test(cfg: &Config, work: &Path, test: &str) -> Result<(String, i32, u64, String, u64, u64), String> {
    let started = std::time::Instant::now();
    let out = Command::new(&cfg.cargo)
        .args(["+1.91.0", "test", "--offline", "--color", "never", "--manifest-path"])
        .arg(work.join("Cargo.toml"))
        .args(["--", "--exact", test])
        .env("CARGO_TARGET_DIR", &cfg.test_target_dir)
        .env("CARGO_NET_OFFLINE", "true")
        .env_remove("RUSTFLAGS")
        .output()
        .map_err(|e| format!("cargo: {e}"))?;
    let mut text = String::from_utf8_lossy(&out.stdout).to_string();
    text.push_str(&String::from_utf8_lossy(&out.stderr));
    // The named test's own line, then the result line of the suite that holds it.
    let mut status = "not_found".to_owned();
    let (mut passed, mut failed) = (0u64, 0u64);
    let mut seen = false;
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix(&format!("test {test} ... ")) {
            status = rest.trim().to_owned();
            seen = true;
        } else if seen && line.starts_with("test result: ") {
            for part in line["test result: ".len()..].split(';') {
                let w: Vec<&str> = part.split_whitespace().collect();
                for pair in w.windows(2) {
                    match pair[1] {
                        "passed" => passed += pair[0].parse().unwrap_or(0),
                        "failed" => failed += pair[0].parse().unwrap_or(0),
                        _ => {}
                    }
                }
            }
            break;
        }
    }
    Ok((status, out.status.code().unwrap_or(-1), started.elapsed().as_millis() as u64, text, passed, failed))
}

#[derive(Clone)]
pub struct Endpoint {
    role: char,
    link: &'static str,
    gate: Arc<Gate>,
    run_dir: PathBuf,
    downstream: Option<Peer<RoleClient>>,
}
impl Endpoint {
    /// A handler-side refusal is a durable journal record before it is an error reply.
    fn refuse(&self, code: &str, request_id: &str, detail: &str) -> ErrorData {
        let _ = self.gate.durable("handler_refused", json!({"link": self.link, "role": self.role.to_string(), "request_id": request_id, "code": code, "detail": detail}));
        refused(if detail.is_empty() { code.to_owned() } else { format!("{code}: {detail}") })
    }
    /// A handler-side failure likewise; the request is answered with an internal error.
    fn fail(&self, code: &str, request_id: &str, detail: impl std::fmt::Display) -> ErrorData {
        let detail = detail.to_string();
        let _ = self.gate.durable("handler_failed", json!({"link": self.link, "role": self.role.to_string(), "request_id": request_id, "code": code, "detail": detail}));
        failed(format!("{code}: {detail}"))
    }
    /// Make one child call through this role's own gate and interpret what came back.
    async fn child(&self, node: &str, arguments: Value) -> Result<CallToolResponse, ErrorData> {
        let gate = &self.gate;
        let peer = self.downstream.as_ref().ok_or_else(|| failed("no downstream link"))?;
        let tool = tool_of(match node { "fix-A" => 'A', _ => 'B' }).0;
        let request: CallToolRequestParams = serde_json::from_value(json!({"name": tool, "arguments": arguments})).map_err(failed)?;
        let call = tokio::time::timeout(Duration::from_millis(gate.cfg.call_ms), peer.call_tool(request)).await;
        let unknown = |terminal: &str| {
            gate.note("child_terminal", json!({"node": node, "terminal": terminal, "effect": "unknown"}));
            json!({"outcome": {"node": node, "effect": {"state": "unknown", "downstream_terminal": terminal}}})
        };
        match call {
            Ok(Ok(result)) => {
                let value = serde_json::to_value(result).map_err(failed)?;
                let (payload, is_error) = payload_of(&value).map_err(failed)?;
                let state = payload["outcome"]["effect"]["state"].clone();
                gate.note("child_terminal", json!({"node": node, "terminal": "response", "effect": state, "is_error": is_error}));
                Ok(tool_result(payload, is_error).into())
            }
            // Nothing reached the child: this role's own gate refused the frame, durably, before release.
            Ok(Err(e)) if !gate.was_released(node) && gate.refused(node) && !gate.write_failed() => {
                gate.note("child_terminal", json!({"node": node, "terminal": "refused_before_release", "effect": "not_dispatched"}));
                let data = json!({"effect": "not_dispatched", "refusals": gate.refusals()});
                Err(ErrorData::invalid_request(format!("CHILD_REFUSED {e}"), Some(data)))
            }
            Ok(Err(e)) if !gate.was_released(node) => {
                // The child's own error reply, before it did anything (its handler refused or a parent refusal arrived through it).
                if let ServiceError::McpError(err) = &e {
                    if err.data.as_ref().is_some_and(|d| d["effect"] == json!("not_dispatched")) {
                        gate.note("child_terminal", json!({"node": node, "terminal": "child_refused_downstream", "effect": "not_dispatched"}));
                        return Err(ErrorData::invalid_request(format!("CHILD_REFUSED {}", err.message), err.data.clone()));
                    }
                }
                Ok(tool_result(unknown("send_failed_after_decision"), true).into())
            }
            Ok(Err(ServiceError::McpError(err))) => {
                if err.data.as_ref().is_some_and(|d| d["effect"] == json!("not_dispatched")) {
                    // The frame left, the child entered, and the child's own gate refused its child: nothing was written downstream.
                    gate.note("child_terminal", json!({"node": node, "terminal": "child_refused_downstream", "effect": "not_dispatched"}));
                    return Err(ErrorData::invalid_request(format!("CHILD_REFUSED {}", err.message), err.data.clone()));
                }
                Ok(tool_result(unknown("protocol_error"), true).into())
            }
            Ok(Err(_)) => Ok(tool_result(unknown("transport_lost"), true).into()),
            Err(_) => Ok(tool_result(unknown("timed_out"), true).into()),
        }
    }
}
impl ServerHandler for Endpoint {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build()).with_protocol_version(ProtocolVersion::V_2025_11_25)
    }
    async fn list_tools(&self, _: Option<PaginatedRequestParams>, _: RequestContext<RoleServer>) -> Result<ListToolsResult, ErrorData> {
        let (name, description) = tool_of(self.role);
        let mut schema = JsonObject::new();
        schema.insert("type".into(), json!("object"));
        let mut result = ListToolsResult::default();
        result.tools = vec![Tool::new(name, description, Arc::new(schema))];
        if self.role == 'B' {
            // Listed on purpose: a capability the policy never permits for a public destination.
            let mut s = JsonObject::new();
            s.insert("type".into(), json!("object"));
            result.tools.push(Tool::new("tests.upload_gist", "Upload a file to a public gist.", Arc::new(s)));
        }
        Ok(result)
    }
    async fn call_tool(&self, request: CallToolRequestParams, context: RequestContext<RoleServer>) -> Result<CallToolResponse, ErrorData> {
        let gate = &self.gate;
        let id = serde_json::to_string(&context.id).map_err(failed)?;
        let tool = request.name.to_string();
        if tool != tool_of(self.role).0 {
            return Err(self.refuse("UNKNOWN_TOOL", &id, &tool));
        }
        let args = Value::Object(request.arguments.clone().unwrap_or_default());
        let dest = args["destination"].as_str().unwrap_or("");
        let test = args["test"].as_str().unwrap_or("");
        if !plain(dest) || !plain(test) {
            return Err(self.refuse("MALFORMED_ARGUMENTS", &id, ""));
        }
        if !gate.plan_verified {
            return Err(self.refuse("PLAN_NOT_VERIFIED", &id, ""));
        }
        // Every role requires the whole received request to equal its checked plan node (ZT-R17).
        // B resolves the plan's commit reference with the commit it was handed, then binds that
        // commit to the workspace below: HEAD must be it and the tree must be clean.
        let node = match self.role { 'G' => "fix-G", 'A' => "fix-A", _ => "verify-B" };
        let commit = args["commit"].as_str().unwrap_or("");
        if self.role == 'B' && !hex_commit(commit) {
            return Err(self.refuse("MALFORMED_COMMIT", &id, ""));
        }
        let expected = {
            let mut inputs = gate.inputs.lock().map_err(|_| failed("lock"))?.clone();
            if self.role == 'B' {
                inputs.insert("fix-A.commit".to_owned(), json!(commit));
            }
            gate.plan.expected_action(node, &inputs)
        };
        match expected {
            Ok(expected) if canonical(&expected) == canonical(&json!({"name": tool, "arguments": args})) => {}
            Ok(_) => return Err(self.refuse("EXACT_ACTION_MISMATCH", &id, "")),
            Err(e) => return Err(self.refuse("UNRESOLVED_INPUT", &id, &e)),
        }
        gate.enter_handler(self.link, &id, &tool, &args).map_err(refused)?;
        match self.role {
            'G' => {
                // The gateway forwards the admitted action to the workspace through its own gate.
                let arguments = json!({"patch": args["patch"], "test": test, "destination": dest});
                self.child("fix-A", arguments).await
            }
            'A' => {
                let work = self.run_dir.join("workspace");
                let (commit, edits) = apply_and_commit(&work, &args["patch"]).map_err(|e| self.fail("COMMIT_FAILED", &id, e))?;
                gate.durable("commit_made", json!({"commit": commit, "edits": edits, "message": args["patch"]["message"]})).map_err(failed)?;
                gate.observe_input("fix-A.commit", json!(commit));
                let mut arguments = json!({"commit": commit, "test": test, "destination": dest});
                if let Some(destination) = &gate.cfg.faults.a_child_destination {
                    // Named fault: the workspace's actual child differs from its checked plan.
                    arguments["destination"] = json!(destination);
                }
                if let Some(other) = &gate.cfg.faults.a_child_test {
                    // Named fault: the same destination, a different test; only the exact-action rule can see it.
                    arguments["test"] = json!(other);
                }
                self.child("verify-B", arguments).await
            }
            _ => {
                let work = self.run_dir.join("workspace");
                let head = git(&work, &["rev-parse", "HEAD"]).map_err(|e| self.fail("GIT_FAILED", &id, e))?;
                if head != commit {
                    return Err(self.refuse("COMMIT_MISMATCH", &id, &format!("workspace is at {head}")));
                }
                let dirty = git(&work, &["status", "--porcelain", "--untracked-files=all"]).map_err(|e| self.fail("GIT_FAILED", &id, e))?;
                if !dirty.is_empty() {
                    return Err(self.refuse("TREE_NOT_AT_COMMIT", &id, &dirty));
                }
                let (status, exit, ms, log, passed, failed_n) = run_test(&gate.cfg, &work, test).map_err(|e| self.fail("CARGO_FAILED", &id, e))?;
                if status != "ok" && status != "FAILED" {
                    // The named test did not run (unknown name, or the build failed): nothing is recorded as a result.
                    let code = if exit == 0 { "TEST_NOT_FOUND" } else { "TEST_BUILD_FAILED" };
                    let tail: String = log.chars().rev().take(300).collect::<Vec<_>>().into_iter().rev().collect();
                    return Err(self.refuse(code, &id, &format!("exit {exit}: {}", tail.trim())));
                }
                // The last 64 KiB of the test's output stay in B's directory, hashed into the record; only the summary leaves.
                let bytes = log.as_bytes();
                let mut start = bytes.len().saturating_sub(65536);
                while start < bytes.len() && !log.is_char_boundary(start) {
                    start += 1;
                }
                let kept = &bytes[start..];
                let output_sha = sha256_hex(kept);
                write_new(&self.run_dir.join("B").join("test-output.txt"), kept).map_err(|e| self.fail("TEST_OUTPUT_NOT_RETAINED", &id, e))?;
                gate.durable("tests_ran", json!({"commit": commit, "test": test, "status": status, "exit": exit, "passed": passed, "failed": failed_n, "duration_ms": ms, "log_bytes": log.len(), "output_sha256": output_sha, "output_bytes": kept.len(), "output_truncated": start > 0, "tree_clean": true}))
                    .map_err(failed)?;
                // The summary: a closed, small text.
                let summary = format!("jsonlite summary\ncommit {commit}\ntest {test}: {status}\npassed {passed} failed {failed_n}\nexit {exit}\n");
                let (dir, path) = match dest {
                    "internal" => (work.join("checks"), self.run_dir.join(SUMMARY_PATH)),
                    other => (self.run_dir.join("B").join(other), self.run_dir.join("B").join(other).join("jsonlite-summary.txt")),
                };
                fs::create_dir_all(&dir).map_err(|e| self.fail("SUMMARY_DIR_FAILED", &id, e))?;
                write_new(&path, summary.as_bytes()).map_err(|e| self.fail("SUMMARY_WRITE_FAILED", &id, e))?;
                File::open(&dir).and_then(|d| d.sync_all()).map_err(|e| self.fail("SUMMARY_DIR_SYNC_FAILED", &id, e))?;
                gate.note("sink_sync", json!({"destination": dest, "file": true, "directory": true}));
                let summary_sha = sha256_hex(summary.as_bytes());
                let relative = path.strip_prefix(&self.run_dir).map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
                let receipt = json!({"schema": "poc.summary-receipt/1", "sink_actor": "tests", "binding": {"commit": commit, "test": test, "status": status, "destination": dest, "path": relative, "summary_sha256": summary_sha}});
                let receipt_bytes = canonical(&receipt);
                let receipt_ref = sha256_hex(&receipt_bytes);
                let receipts = self.run_dir.join("B").join("receipts");
                fs::create_dir_all(&receipts).map_err(|e| self.fail("RECEIPT_DIR_FAILED", &id, e))?;
                write_new(&receipts.join(format!("{receipt_ref}.json")), &receipt_bytes).map_err(|e| self.fail("RECEIPT_WRITE_FAILED", &id, e))?;
                gate.durable("summary_written", json!({"destination": dest, "path": relative, "summary_sha256": summary_sha, "receipt_ref": receipt_ref})).map_err(failed)?;
                let payload = json!({"outcome": {"commit": commit, "test": test, "status": status, "passed": passed, "failed": failed_n, "destination": dest,
                                               "effect": {"state": "known_applied", "receipt_ref": format!("sha256:{receipt_ref}")}}});
                Ok(tool_result(payload, false).into())
            }
        }
    }
}

pub fn client_config() -> ClientConfig {
    ClientConfig::default().with_protocol_version(ProtocolVersion::V_2025_11_25)
}

/// Run one server role until its client closes the link. G starts A and A starts B.
pub async fn serve(role: char, run_dir: &Path) -> Result<(), Error> {
    let cfg = Arc::new(Config::load(run_dir)?);
    let plan = Arc::new(Plan::load(&cfg)?);
    let dir = run_dir.join(role.to_string());
    let journal = Journal::create(&dir, &role.to_string())?;
    let gate = Gate::new(role, cfg.clone(), journal, plan.clone(), &dir);
    gate.note("role_started", json!({"role": role.to_string(), "pid": std::process::id(), "scene": cfg.scene}));
    // Every role checks the whole plan itself and records its verdict before it serves anything (ZT-R17).
    let assessment = plan.check();
    gate.durable("plan_checked", json!({"program_id": plan.id(), "nodes": plan.nodes().len(), "verdict": assessment.verdict,
                                        "codes": crate::plan::codes(&assessment.findings), "role": role.to_string()}))?;
    let (link, child_role) = match role {
        'G' => ("H-G", Some('A')),
        'A' => ("G-A", Some('B')),
        _ => ("A-B", None),
    };
    if role == 'A' {
        let base = prepare_workspace(Path::new(&cfg.fixture_dir), &run_dir.join("workspace"))?;
        gate.durable("workspace_ready", json!({"base_commit": base, "fixture": "jsonlite"}))?;
    }
    let mut endpoint = Endpoint { role, link, gate: gate.clone(), run_dir: run_dir.to_path_buf(), downstream: None };
    let mut child = None;
    if let Some(next) = child_role {
        fs::create_dir_all(run_dir.join(next.to_string()))?;
        let stderr = File::create(run_dir.join(next.to_string()).join("stderr.log"))?;
        let mut process = tokio::process::Command::new(&cfg.exe)
            .args(["serve", "--role", &next.to_string(), "--run-dir"])
            .arg(run_dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(stderr))
            .kill_on_drop(true)
            .spawn()?;
        let (stdin, stdout) = (process.stdin.take().ok_or("child stdin")?, process.stdout.take().ok_or("child stdout")?);
        let setup = Duration::from_millis(cfg.setup_ms);
        let down_link: &'static str = if role == 'G' { "G-A" } else { "A-B" };
        let client = tokio::time::timeout(setup, client_config().serve(GatedTransport::new(stdout, stdin, gate.clone(), down_link))).await??;
        let negotiated = client.peer_info().map(|i| json!(i.as_ref())).unwrap_or(Value::Null);
        if negotiated["protocolVersion"] != json!("2025-11-25") {
            return Err(format!("{down_link}: negotiated protocol is {}", negotiated["protocolVersion"]).into());
        }
        let tools = tokio::time::timeout(setup, client.peer().list_tools(None)).await??;
        let names: Vec<String> = tools.tools.iter().map(|t| t.name.to_string()).collect();
        gate.durable("link_ready", json!({"link": down_link, "tools": names, "protocol": negotiated["protocolVersion"]}))?;
        endpoint.downstream = Some(client.peer().clone());
        child = Some((next, process, client));
    }
    let service = endpoint.serve(GatedTransport::new(tokio::io::stdin(), tokio::io::stdout(), gate.clone(), link)).await?;
    let quit = service.waiting().await?;
    gate.note("server_stopped", json!({"link": link, "reason": format!("{quit:?}")}));
    if let Some((next, mut process, client)) = child {
        let _ = client.cancel().await;
        let status = tokio::time::timeout(Duration::from_millis(cfg.cleanup_ms), process.wait()).await;
        let code = match status {
            Ok(Ok(status)) => json!(status.code()),
            _ => {
                let _ = process.kill().await;
                json!("killed_after_cleanup_deadline")
            }
        };
        gate.durable("child_exit", json!({"role": next.to_string(), "status": code}))?;
    }
    Ok(())
}
