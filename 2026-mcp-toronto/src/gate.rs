//! The single path for every outgoing MCP frame of a role.
//!
//! rmcp builds the message (request id, `_meta.progressToken`, result
//! envelope). `GatedTransport::send` serializes it once, and that exact buffer
//! is what the gate decides on, durably consumes and then writes. Nothing is
//! re-serialized after the decision (ZT-R12, ZT-R14).
use crate::{
    journal::{Ack, Journal},
    plan::{Finding, Inputs, Plan},
    util::{Config, canonical, sha256_hex},
};
use rmcp::{
    service::{RxJsonRpcMessage, ServiceRole, TxJsonRpcMessage},
    transport::Transport,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    io,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    sync::oneshot,
};

/// MCP frame bound, including the final LF.
pub const MAX_FRAME: usize = 65_536;
const CONTROL_REQUESTS: [&str; 3] = ["initialize", "tools/list", "ping"];
const CONTROL_NOTIFICATIONS: [&str; 3] = ["notifications/initialized", "notifications/cancelled", "notifications/progress"];

/// The checked Program node a business call on this link must equal.
pub fn link_node(role: char, link: &str) -> Option<&'static str> {
    match (role, link) {
        ('H', "H-G") => Some("fix-G"),
        ('G', "G-A") => Some("fix-A"),
        ('A', "A-B") => Some("verify-B"),
        _ => None,
    }
}

/// Permission to write one exact frame once. Only `Gate` constructs it, and
/// only after the consumption record is synced.
pub struct Permit {
    class: &'static str,
    node: Option<&'static str>,
    frame_sha256: String,
    _ack: Ack,
}

#[derive(Clone)]
struct Incoming {
    frame_sha256: String,
    method: String,
}

pub struct Gate {
    pub role: char,
    pub cfg: Arc<Config>,
    pub plan: Arc<Plan>,
    /// The whole-plan verdict this role computed itself at start (ZT-R17).
    pub plan_verified: bool,
    pub inputs: Mutex<Inputs>,
    journal: Mutex<Journal>,
    used: Mutex<BTreeSet<String>>,
    incoming: Mutex<BTreeMap<String, Incoming>>,
    answered: Mutex<BTreeSet<String>>,
    refusals: Mutex<Vec<Value>>,
    released: Mutex<BTreeSet<String>>,
    /// Where this role retains the exact bytes of every gated business frame.
    frames: std::path::PathBuf,
    write_failed: AtomicBool,
}

fn finding(rule: &str, reason: &str, node: &str) -> Finding {
    Finding { rule: rule.into(), reason: reason.into(), node: node.into() }
}

impl Gate {
    pub fn new(role: char, cfg: Arc<Config>, journal: Journal, plan: Arc<Plan>, dir: &std::path::Path) -> Arc<Self> {
        let plan_verified = plan.check().verdict == "verified";
        let frames = dir.join("frames");
        let _ = std::fs::create_dir_all(&frames);
        Arc::new(Self {
            frames,
            write_failed: AtomicBool::new(false),
            role,
            cfg,
            plan,
            plan_verified,
            inputs: Mutex::default(),
            journal: Mutex::new(journal),
            used: Mutex::default(),
            incoming: Mutex::default(),
            answered: Mutex::default(),
            refusals: Mutex::default(),
            released: Mutex::default(),
        })
    }
    pub fn durable(&self, kind: &str, body: Value) -> Result<Ack, String> {
        self.journal
            .lock()
            .map_err(|_| "journal lock poisoned".to_owned())?
            .durable(kind, body)
            .map_err(|e| format!("STORAGE_FAILED: {e}"))
    }
    pub fn note(&self, kind: &str, body: Value) {
        if let Ok(mut journal) = self.journal.lock() {
            let _ = journal.note(kind, body);
        }
    }
    /// Record an observed input (`node.port`) for later exact-action checks.
    pub fn observe_input(&self, key: &str, value: Value) {
        if let Ok(mut inputs) = self.inputs.lock() {
            inputs.insert(key.to_owned(), value);
        }
    }
    pub fn refusals(&self) -> Vec<Value> {
        self.refusals.lock().map(|r| r.clone()).unwrap_or_default()
    }
    /// True once the business frame for `node` reached the writer.
    pub fn was_released(&self, node: &str) -> bool {
        self.released.lock().is_ok_and(|r| r.contains(node))
    }
    fn refuse(&self, link: &str, seq: u64, class: &str, node: Option<&str>, destination: Option<&str>, sha: &str, frame: Option<&[u8]>, findings: Vec<Finding>) -> String {
        let codes: Vec<String> = findings.iter().map(Finding::code).collect();
        // The record names what was refused, destination included, so a reader needs no frame bytes to see where it pointed.
        let record = json!({"link": link, "seq": seq, "class": class, "node": node, "destination": destination, "frame_sha256": sha,
                            "findings": findings.iter().map(Finding::json).collect::<Vec<_>>()});
        if let Some(bytes) = frame {
            let _ = crate::util::write_new(&self.frames.join(format!("refused-{sha}.json")), bytes);
        }
        // A refusal that could not be recorded is not a refusal anyone may rely on.
        if let Err(e) = self.durable("refused", record.clone()) {
            return format!("STORAGE_FAILED: the refusal could not be recorded ({e})");
        }
        if let Ok(mut refusals) = self.refusals.lock() {
            refusals.push(record);
        }
        format!("REFUSED before release: {}", codes.join("; "))
    }
    /// True when a durable refusal record exists for this node.
    pub fn refused(&self, node: &str) -> bool {
        self.refusals.lock().is_ok_and(|r| r.iter().any(|x| x["node"] == json!(node)))
    }
    pub fn write_failed(&self) -> bool {
        self.write_failed.load(Ordering::SeqCst)
    }
    fn note_write_failed(&self) {
        self.write_failed.store(true, Ordering::SeqCst);
    }

    /// Decide on the final serialized frame and durably consume the decision.
    pub fn admit_outgoing(&self, link: &'static str, seq: u64, frame: &[u8]) -> Result<Permit, String> {
        let sha = sha256_hex(frame);
        if frame.len() > MAX_FRAME {
            return Err(self.refuse(link, seq, "oversize", None, None, &sha, None, vec![finding("ZT-R03", "FRAME_TOO_LARGE", link)]));
        }
        let v: Value = serde_json::from_slice(&frame[..frame.len() - 1]).map_err(|e| e.to_string())?;
        let method = v.get("method").and_then(Value::as_str);
        let has_id = v.get("id").is_some();
        match (method, has_id) {
            (Some("tools/call"), true) => self.admit_business(link, seq, &v, frame, sha),
            (Some(m), true) if CONTROL_REQUESTS.contains(&m) => self.admit_control(link, seq, m, sha, frame.len()),
            (Some(m), false) if CONTROL_NOTIFICATIONS.contains(&m) => self.admit_control(link, seq, m, sha, frame.len()),
            (None, true) if v.get("result").is_some() || v.get("error").is_some() => self.admit_response(link, seq, &v, sha),
            _ => Err(self.refuse(link, seq, "unsupported", None, None, &sha, None, vec![finding("ZT-R15", "UNSUPPORTED_MESSAGE", link)])),
        }
    }

    fn admit_control(&self, link: &str, seq: u64, method: &str, sha: String, bytes: usize) -> Result<Permit, String> {
        let ack = self.durable("control_consumed", json!({"link": link, "seq": seq, "method": method, "frame_sha256": sha, "bytes": bytes}))?;
        Ok(Permit { class: "control", node: None, frame_sha256: sha, _ack: ack })
    }

    fn admit_response(&self, link: &str, seq: u64, v: &Value, sha: String) -> Result<Permit, String> {
        let key = format!("{link}|{}", v["id"]);
        let request = self.incoming.lock().ok().and_then(|m| m.get(&key).cloned());
        let Some(request) = request else {
            return Err(self.refuse(link, seq, "response", None, None, &sha, None, vec![finding("ZT-R15", "UNSOLICITED_RESPONSE", link)]));
        };
        if !self.answered.lock().map_err(|_| "lock")?.insert(key) {
            return Err(self.refuse(link, seq, "response", None, None, &sha, None, vec![finding("ZT-R14", "DUPLICATE_RESPONSE", link)]));
        }
        let is_error = v.get("error").is_some() || v["result"]["isError"] == json!(true);
        let ack = self.durable(
            "response_consumed",
            json!({"link": link, "seq": seq, "request_id": v["id"], "method": request.method,
                   "request_frame_sha256": request.frame_sha256, "frame_sha256": sha, "is_error": is_error}),
        )?;
        Ok(Permit { class: "response", node: None, frame_sha256: sha, _ack: ack })
    }

    fn admit_business(&self, link: &'static str, seq: u64, v: &Value, frame: &[u8], sha: String) -> Result<Permit, String> {
        let Some(node) = link_node(self.role, link) else {
            return Err(self.refuse(link, seq, "business", None, v["params"]["arguments"]["destination"].as_str(), &sha, Some(frame), vec![finding("ZT-R07", "NO_BUSINESS_CALL_ON_THIS_LINK", link)]));
        };
        let params = &v["params"];
        let mut findings = Vec::new();
        // The SDK adds metadata after the caller built the request. Admit only
        // the enumerated members; none of them carries authority.
        for key in params.as_object().map(|o| o.keys().cloned().collect::<Vec<_>>()).unwrap_or_default() {
            if !["name", "arguments", "_meta"].contains(&key.as_str()) {
                findings.push(finding("ZT-R12", &format!("UNADMITTED_PARAM {key}"), node));
            }
        }
        for key in params["_meta"].as_object().map(|o| o.keys().cloned().collect::<Vec<_>>()).unwrap_or_default() {
            if key != "progressToken" {
                findings.push(finding("ZT-R12", &format!("UNADMITTED_META {key}"), node));
            }
        }
        if !self.plan_verified {
            findings.push(finding("ZT-R17", "PLAN_NOT_VERIFIED", node));
        }
        let actual = json!({"name": params["name"], "arguments": params["arguments"]});
        {
            let inputs = self.inputs.lock().map_err(|_| "lock")?;
            findings.extend(self.plan.check_action(node, &inputs, &actual));
        }
        if self.used.lock().map_err(|_| "lock")?.contains(node) {
            findings.push(finding("ZT-R14", "DUPLICATE_ADMISSION", node));
        }
        let destination = params["arguments"]["destination"].as_str();
        if !findings.is_empty() {
            return Err(self.refuse(link, seq, "business", Some(node), destination, &sha, Some(frame), findings));
        }
        let tool = params["name"].as_str().unwrap_or("");
        // The exact bytes decided on are retained beside the journal, keyed by their hash.
        crate::util::write_new(&self.frames.join(format!("released-{sha}.json")), frame).map_err(|e| format!("STORAGE_FAILED: {e}"))?;
        // The durable cut. Without its Ack there is no Permit.
        let ack = self.durable(
            "business_consumed",
            json!({"link": link, "seq": seq, "node": node, "tool": tool, "destination": destination,
                   "frame_sha256": sha, "bytes": frame.len(), "meta": params.get("_meta")}),
        )?;
        self.used.lock().map_err(|_| "lock")?.insert(node.to_owned());
        Ok(Permit { class: "business", node: Some(node), frame_sha256: sha, _ack: ack })
    }

    /// Called after the admitted buffer was written and flushed.
    pub fn released(&self, permit: Permit, link: &str, seq: u64, bytes: usize) {
        self.note(
            "released",
            json!({"link": link, "seq": seq, "class": permit.class, "node": permit.node, "frame_sha256": permit.frame_sha256, "bytes": bytes}),
        );
        if let (Some(node), Ok(mut released)) = (permit.node, self.released.lock()) {
            released.insert(node.to_owned());
        }
    }

    /// Record a received frame. Receiving authorizes nothing.
    pub fn observe_incoming(&self, link: &str, line: &[u8]) {
        let sha = sha256_hex(line);
        let Ok(v) = serde_json::from_slice::<Value>(&line[..line.len() - 1]) else { return };
        let method = v.get("method").and_then(Value::as_str).unwrap_or("");
        let kind = match (v.get("method").is_some(), v.get("id").is_some()) {
            (true, true) => "request",
            (true, false) => "notification",
            _ => "response",
        };
        if kind == "request" {
            if let Ok(mut incoming) = self.incoming.lock() {
                incoming.insert(format!("{link}|{}", v["id"]), Incoming { frame_sha256: sha.clone(), method: method.to_owned() });
            }
        }
        self.note("frame_received", json!({"link": link, "kind": kind, "method": method, "id": v.get("id"), "frame_sha256": sha, "bytes": line.len()}));
    }

    /// Server-side admission of one handler entry for the exact received frame.
    pub fn enter_handler(&self, link: &str, request_id: &str, tool: &str, args: &Value) -> Result<(), String> {
        let key = format!("{link}|{request_id}");
        let request = self.incoming.lock().ok().and_then(|m| m.get(&key).cloned());
        let node = format!("handler:{tool}");
        let mut findings = Vec::new();
        if request.is_none() {
            findings.push(finding("ZT-R14", "NO_RECEIVED_FRAME_FOR_HANDLER", &node));
        }
        if self.used.lock().map_err(|_| "lock")?.contains(&node) {
            findings.push(finding("ZT-R14", "DUPLICATE_HANDLER_ENTRY", &node));
        }
        if !self.plan.may_serve(self.role, tool, args["destination"].as_str()) {
            findings.push(finding("ZT-R07", "CAPABILITY_WITHOUT_PERMISSION", &node));
        }
        if !findings.is_empty() {
            return Err(self.refuse(link, 0, "handler", None, args["destination"].as_str(), "", None, findings));
        }
        self.durable(
            "handler_entry",
            json!({"link": link, "role": self.role.to_string(), "tool": tool, "request_id": request_id,
                   "frame_sha256": request.map(|r| r.frame_sha256), "args_sha256": sha256_hex(&canonical(args))}),
        )?;
        self.used.lock().map_err(|_| "lock")?.insert(node);
        Ok(())
    }
}

/// Dropped without completing: the write was aborted after the decision.
struct Armed {
    gate: Arc<Gate>,
    link: &'static str,
    seq: u64,
    retired: Arc<AtomicBool>,
    done: bool,
}
impl Drop for Armed {
    fn drop(&mut self) {
        if !self.done {
            self.retired.store(true, Ordering::SeqCst);
            self.gate.note_write_failed();
            self.gate.note("send_aborted", json!({"link": self.link, "seq": self.seq, "after": "decision, before the write completed"}));
        }
    }
}

/// rmcp transport over one byte stream pair, gated on every send.
pub struct GatedTransport<Rd, Wr> {
    reader: Rd,
    writer: Arc<tokio::sync::Mutex<Wr>>,
    gate: Arc<Gate>,
    link: &'static str,
    next_seq: u64,
    tail: Option<oneshot::Receiver<()>>,
    inbuf: Vec<u8>,
    retired: Arc<AtomicBool>,
}
impl<Rd, Wr> GatedTransport<Rd, Wr> {
    pub fn new(reader: Rd, writer: Wr, gate: Arc<Gate>, link: &'static str) -> Self {
        Self {
            reader,
            writer: Arc::new(tokio::sync::Mutex::new(writer)),
            gate,
            link,
            next_seq: 0,
            tail: None,
            inbuf: Vec::new(),
            retired: Arc::new(AtomicBool::new(false)),
        }
    }
}
impl<R, Rd, Wr> Transport<R> for GatedTransport<Rd, Wr>
where
    R: ServiceRole,
    Rd: AsyncRead + Unpin + Send + 'static,
    Wr: AsyncWrite + Unpin + Send + 'static,
{
    type Error = io::Error;

    fn send(&mut self, item: TxJsonRpcMessage<R>) -> impl Future<Output = Result<(), Self::Error>> + Send + 'static {
        // Sequence and final bytes are fixed synchronously at send entry.
        self.next_seq += 1;
        let seq = self.next_seq;
        let frame = serde_json::to_vec(&item).map(|mut bytes| {
            bytes.push(b'\n');
            bytes
        });
        // Frames release in sequence order: each waits for its predecessor.
        let previous = self.tail.take();
        let (done, next) = oneshot::channel::<()>();
        self.tail = Some(next);
        let (gate, writer, link, retired) = (self.gate.clone(), self.writer.clone(), self.link, self.retired.clone());
        async move {
            if let Some(previous) = previous {
                let _ = previous.await;
            }
            let _done = done;
            let frame = frame.map_err(io::Error::other)?;
            if retired.load(Ordering::SeqCst) {
                return Err(io::Error::other("link retired after a failed write"));
            }
            let permit = gate.admit_outgoing(link, seq, &frame).map_err(io::Error::other)?;
            // If this future is dropped between the decision and the end of the write,
            // bytes may be half on the wire: retire the link and say so in the journal.
            let mut guard = Armed { gate: gate.clone(), link, seq, retired: retired.clone(), done: false };
            let mut writer = writer.lock().await;
            let written = async {
                writer.write_all(&frame).await?;
                writer.flush().await
            }
            .await;
            guard.done = true;
            match written {
                Ok(()) => {
                    gate.released(permit, link, seq, frame.len());
                    Ok(())
                }
                Err(e) => {
                    // Bytes may have left; this link never sends again.
                    retired.store(true, Ordering::SeqCst);
                    gate.note_write_failed();
                    gate.note("write_failed", json!({"link": link, "seq": seq, "error": e.to_string()}));
                    Err(e)
                }
            }
        }
    }

    async fn receive(&mut self) -> Option<RxJsonRpcMessage<R>> {
        loop {
            if let Some(end) = self.inbuf.iter().position(|b| *b == b'\n') {
                let line: Vec<u8> = self.inbuf.drain(..=end).collect();
                return match serde_json::from_slice::<RxJsonRpcMessage<R>>(&line[..line.len() - 1]) {
                    Ok(message) => {
                        self.gate.observe_incoming(self.link, &line);
                        Some(message)
                    }
                    Err(e) => {
                        self.gate.note("malformed_frame", json!({"link": self.link, "error": e.to_string(), "bytes": line.len()}));
                        None
                    }
                };
            }
            // Refuse before accumulating more than one maximal frame.
            if self.inbuf.len() >= MAX_FRAME {
                self.gate.note("frame_too_large", json!({"link": self.link, "buffered": self.inbuf.len()}));
                return None;
            }
            let mut chunk = [0u8; 4096];
            let room = (MAX_FRAME - self.inbuf.len()).min(chunk.len());
            match self.reader.read(&mut chunk[..room]).await {
                Ok(0) | Err(_) => {
                    if !self.inbuf.is_empty() {
                        self.gate.note("incomplete_frame_at_eof", json!({"link": self.link, "buffered": self.inbuf.len()}));
                    }
                    self.gate.note("link_closed", json!({"link": self.link}));
                    return None;
                }
                Ok(n) => self.inbuf.extend_from_slice(&chunk[..n]),
            }
        }
    }

    async fn close(&mut self) -> Result<(), Self::Error> {
        self.retired.store(true, Ordering::SeqCst);
        self.writer.lock().await.shutdown().await
    }
}
