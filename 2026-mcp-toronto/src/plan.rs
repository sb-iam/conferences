//! The checked Program, the policy, the consent, and the two checks that make
//! the point of this demo: the whole plan before the first call, and the exact
//! action at a gate before its bytes leave.
//!
//! A Program is three nodes in JSON (`fixtures/program-*.json`). Arguments may
//! refer to an earlier node's output (`{"$ref": "node.port"}`) or derive from
//! it (`{"$derive": "summary", "of": "node.port"}`). The policy
//! (`fixtures/policy.json`) names permitted calls, permitted handler entries
//! and sink clearances. The consent (`fixtures/consent.json`) is the admitted
//! intent: which effects the user asked for. A proposal cannot amend it.
use crate::util::{Config, Error, canonical, read_json};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path};

/// This demo has no derived argument: the test runner derives its own summary
/// from the run, and the plan binds only the commit it is asked to test.
pub fn summarize(record: &Value) -> Value {
    record.clone()
}

/// Observed inputs so far, keyed `node.port`.
pub type Inputs = BTreeMap<String, Value>;

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Finding {
    pub rule: String,
    pub reason: String,
    pub node: String,
}
impl Finding {
    fn new(rule: &str, reason: &str, node: &str) -> Self {
        Self { rule: rule.into(), reason: reason.into(), node: node.into() }
    }
    pub fn code(&self) -> String {
        format!("{} {}", self.rule, self.reason)
    }
    pub fn json(&self) -> Value {
        json!({"rule_id": self.rule, "reason": self.reason, "status": "violated", "node_id": self.node})
    }
}
/// Distinct finding codes, sorted, for panels and expectations.
pub fn codes(findings: &[Finding]) -> Vec<String> {
    let mut codes: Vec<String> = findings.iter().map(Finding::code).collect();
    codes.sort();
    codes.dedup();
    codes
}

pub struct Assessment {
    pub verdict: &'static str,
    pub findings: Vec<Finding>,
}

pub struct Plan {
    pub program: Value,
    pub policy: Value,
    pub consent: Value,
    pub intent: String,
}

/// Which role offers which tool. Listing a tool is a capability; permission is
/// a policy row (ZT-R07 keeps them apart).
pub fn tool_of(role: char) -> (&'static str, &'static str) {
    match role {
        'G' => ("gateway.submit_fix", "Submit the approved fix: the gateway forwards it to the workspace and relays the verified result."),
        'A' => ("workspace.apply_and_verify", "Apply the approved edits, commit them, and ask the test runner to verify that commit."),
        _ => ("tests.run_and_record", "Run exactly one named test on a commit and record the summary at the named destination."),
    }
}

impl Plan {
    pub fn load(cfg: &Config) -> Result<Self, Error> {
        Ok(Self {
            program: read_json(Path::new(&cfg.program_file))?,
            policy: read_json(Path::new(&cfg.policy_file))?,
            consent: read_json(Path::new(&cfg.consent_file))?,
            intent: std::fs::read_to_string(&cfg.intent_file)?.trim().to_owned(),
        })
    }
    pub fn id(&self) -> &str {
        self.program["program_id"].as_str().unwrap_or("?")
    }
    pub fn nodes(&self) -> &[Value] {
        self.program["nodes"].as_array().map(Vec::as_slice).unwrap_or(&[])
    }
    pub fn node(&self, id: &str) -> Option<&Value> {
        self.nodes().iter().find(|n| n["id"] == json!(id))
    }
    fn role_of(v: &Value) -> char {
        v.as_str().and_then(|s| s.chars().next()).unwrap_or('?')
    }

    /// Resolve `$ref` and `$derive` members against the inputs observed so far.
    fn resolve(&self, value: &Value, inputs: &Inputs) -> Result<Value, String> {
        match value {
            Value::Object(map) if map.contains_key("$ref") => {
                let key = map["$ref"].as_str().ok_or("$ref must be a string")?;
                inputs.get(key).cloned().ok_or_else(|| format!("input {key} not observed yet"))
            }
            Value::Object(map) if map.contains_key("$derive") => {
                let of = map["of"].as_str().ok_or("$derive needs `of`")?;
                let source = inputs.get(of).ok_or_else(|| format!("input {of} not observed yet"))?;
                match map["$derive"].as_str() {
                    Some("summary") => Ok(summarize(source)),
                    other => Err(format!("unknown derivation {other:?}")),
                }
            }
            Value::Object(map) => {
                let mut out = serde_json::Map::new();
                for (k, v) in map {
                    out.insert(k.clone(), self.resolve(v, inputs)?);
                }
                Ok(Value::Object(out))
            }
            Value::Array(items) => Ok(Value::Array(items.iter().map(|v| self.resolve(v, inputs)).collect::<Result<_, _>>()?)),
            other => Ok(other.clone()),
        }
    }
    /// The exact action a node admits, as `{"name", "arguments"}`, given the inputs seen so far.
    pub fn expected_action(&self, node_id: &str, inputs: &Inputs) -> Result<Value, String> {
        let node = self.node(node_id).ok_or_else(|| format!("no node {node_id}"))?;
        Ok(json!({"name": node["tool"], "arguments": self.resolve(&node["arguments"], inputs)?}))
    }
    /// The references a node's arguments make to earlier outputs.
    fn references(value: &Value, out: &mut Vec<String>) {
        match value {
            Value::Object(map) => {
                if let Some(r) = map.get("$ref").and_then(Value::as_str) {
                    out.push(r.to_owned());
                } else if let Some(r) = map.get("of").and_then(Value::as_str).filter(|_| map.contains_key("$derive")) {
                    out.push(r.to_owned());
                } else {
                    map.values().for_each(|v| Self::references(v, out));
                }
            }
            Value::Array(items) => items.iter().for_each(|v| Self::references(v, out)),
            _ => {}
        }
    }
    /// The label of the data a node receives: the label the producing node
    /// declares for the referenced port. Derivation keeps the label (ZT-R09:
    /// restrictions propagate conservatively; summarizing does not declassify).
    pub fn input_label(&self, node: &Value) -> Option<Value> {
        let mut refs = Vec::new();
        Self::references(&node["arguments"], &mut refs);
        for r in refs {
            let (producer, port) = r.split_once('.')?;
            let p = self.node(producer)?;
            if p["produces"]["port"] == json!(port) && p["produces"]["label"].is_object() {
                return Some(p["produces"]["label"].clone());
            }
        }
        None
    }
    fn rank(&self, classification: &Value) -> Option<usize> {
        self.policy["classification_order"].as_array().and_then(|o| o.iter().position(|c| c == classification))
    }
    /// The sink a tool writes to, by the policy's own table, not by the proposal's word.
    fn sink_of_tool(&self, tool: &str) -> Option<String> {
        self.policy["sink_tools"][tool].as_str().map(str::to_owned)
    }
    fn permitted(&self, table: &str, actor: char, tool: &str, destination: Option<&str>) -> bool {
        self.policy[table].as_array().is_some_and(|rows| {
            rows.iter().any(|r| {
                r["actor"] == json!(actor.to_string())
                    && r["tool"] == json!(tool)
                    && (r.get("destination").is_none() || r["destination"].as_str() == destination)
            })
        })
    }
    pub fn may_call(&self, actor: char, tool: &str, destination: Option<&str>) -> bool {
        self.permitted("call", actor, tool, destination)
    }
    pub fn may_serve(&self, actor: char, tool: &str, destination: Option<&str>) -> bool {
        self.permitted("serve", actor, tool, destination)
    }

    /// The rules on one action at one node, evaluated on the arguments given
    /// (the plan's own for the whole-plan check; the actual bytes at a gate).
    fn action_findings(&self, node: &Value, tool: &str, arguments: &Value) -> Vec<Finding> {
        let id = node["id"].as_str().unwrap_or("?");
        let caller = Self::role_of(&node["caller"]);
        let destination = arguments["destination"].as_str();
        let mut findings = Vec::new();
        if !self.may_call(caller, tool, destination) {
            findings.push(Finding::new("ZT-R07", "CAPABILITY_WITHOUT_PERMISSION", id));
        }
        // A node whose tool the policy knows as a sink tool is an effect node whether or
        // not the proposal says so; the proposal cannot opt out of the sink rules.
        let declared = node.get("effect").filter(|e| e.is_object()).and_then(|e| e["sink"].as_str().map(str::to_owned));
        let by_policy = self.sink_of_tool(tool);
        if by_policy.is_some() && declared.is_none() {
            findings.push(Finding::new("ZT-R04", "EFFECT_UNDECLARED", id));
        }
        if let Some(sink) = by_policy.clone().or(declared) {
            match destination.map(|d| &self.policy["sinks"][sink.as_str()][d]).filter(|e| e.is_object()) {
                None => findings.push(Finding::new("ZT-S05", "UNKNOWN_DESTINATION", id)),
                Some(entry) => match self.input_label(node) {
                    None => findings.push(Finding::new("ZT-S05", "LABEL_MISSING", id)),
                    Some(label) => {
                        if entry["tenant"] != label["tenant"] {
                            findings.push(Finding::new("ZT-S05", "LABEL_TENANT", id));
                        }
                        match (self.rank(&entry["clearance"]), self.rank(&label["classification"])) {
                            (Some(clearance), Some(classification)) => {
                                if clearance < classification {
                                    findings.push(Finding::new("ZT-S05", "LABEL_CLASSIFICATION", id));
                                }
                            }
                            _ => findings.push(Finding::new("ZT-S05", "UNKNOWN_CLASSIFICATION", id)),
                        }
                    }
                },
            }
            let allowed = self.consent["allowed_effects"].as_array().is_some_and(|a| {
                a.iter().any(|e| e["sink"] == json!(sink) && e["destination"].as_str() == destination)
            });
            if !allowed {
                findings.push(Finding::new("ZT-R01", "CONSENT_SCOPE_MISMATCH", id));
            }
        }
        findings
    }

    /// The whole plan, before any call (ZT-R17). Structure first, then every
    /// node's action against policy, labels and consent.
    pub fn check(&self) -> Assessment {
        let mut findings = Vec::new();
        for node in self.nodes() {
            let id = node["id"].as_str().unwrap_or("?");
            let callee = Self::role_of(&node["callee"]);
            let tool = node["tool"].as_str().unwrap_or("");
            if tool_of(callee).0 != tool {
                findings.push(Finding::new("ZT-R07", "TOOL_NOT_OFFERED_BY_CALLEE", id));
            }
            let mut refs = Vec::new();
            Self::references(&node["arguments"], &mut refs);
            for r in refs {
                let ok = r.split_once('.').is_some_and(|(producer, port)| {
                    self.nodes().iter().take_while(|n| n["id"] != json!(id)).any(|n| n["id"] == json!(producer) && n["produces"]["port"] == json!(port))
                });
                if !ok {
                    findings.push(Finding::new("ZT-R04", "UNRESOLVED_INPUT", id));
                }
            }
            findings.extend(self.action_findings(node, tool, &node["arguments"]));
        }
        findings.sort();
        findings.dedup();
        Assessment { verdict: if findings.is_empty() { "verified" } else { "denied" }, findings }
    }

    /// The exact action at a gate: the final bytes' `name` and `arguments`
    /// must equal the admitted node (ZT-R12), and the node's rules are
    /// evaluated again on what is actually about to leave (ZT-R17).
    pub fn check_action(&self, node_id: &str, inputs: &Inputs, actual: &Value) -> Vec<Finding> {
        let mut findings = Vec::new();
        let Some(node) = self.node(node_id) else {
            return vec![Finding::new("ZT-R17", "NO_SUCH_NODE", node_id)];
        };
        match self.expected_action(node_id, inputs) {
            Ok(expected) => {
                if canonical(&expected) != canonical(actual) {
                    findings.push(Finding::new("ZT-R12", "EXACT_ACTION_MISMATCH", node_id));
                }
            }
            Err(e) => findings.push(Finding::new("ZT-R04", &format!("UNRESOLVED_INPUT {e}"), node_id)),
        }
        let tool = actual["name"].as_str().unwrap_or("");
        findings.extend(self.action_findings(node, tool, &actual["arguments"]));
        findings.sort();
        findings.dedup();
        findings
    }
}
