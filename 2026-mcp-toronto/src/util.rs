//! Shared run configuration and small helpers.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{fs, io::Write, path::Path};

pub type Error = Box<dyn std::error::Error + Send + Sync>;

/// The one place the internal summary may be written, relative to the run directory.
pub const SUMMARY_PATH: &str = "workspace/checks/jsonlite-summary.txt";

pub fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
pub fn short(value: &str) -> String {
    value.trim_start_matches("sha256:").chars().take(12).collect()
}
/// Canonical bytes of a JSON value. `serde_json::Value` objects are ordered
/// maps (the SDK does not enable `preserve_order`), so this is deterministic.
pub fn canonical(value: &Value) -> Vec<u8> {
    serde_json::to_vec(value).unwrap_or_default()
}

/// Named test faults. Each is selected by the scene, never by a peer.
#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct Faults {
    /// A (the workspace) replaces the destination of its actual child request.
    #[serde(default)]
    pub a_child_destination: Option<String>,
    /// A (the workspace) replaces the test name of its actual child request.
    #[serde(default)]
    pub a_child_test: Option<String>,
}

/// One run's configuration. H writes it into the run directory before it
/// starts A and B; every role reads the same file.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Config {
    pub schema: String,
    pub run_id: String,
    pub scene: String,
    pub exe: String,
    pub program_file: String,
    pub policy_file: String,
    pub consent_file: String,
    pub intent_file: String,
    /// The fixture crate copied into each run's workspace.
    pub fixture_dir: String,
    /// The cargo command B runs the exact test with, and the shared target directory.
    pub cargo: String,
    pub test_target_dir: String,
    pub faults: Faults,
    pub setup_ms: u64,
    pub call_ms: u64,
    pub cleanup_ms: u64,
    /// The viewing pace this run was started with, as a record of the run.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pace_ms: Option<u64>,
}
impl Config {
    pub fn load(run_dir: &Path) -> Result<Self, Error> {
        Ok(serde_json::from_slice(&fs::read(run_dir.join("config.json"))?)?)
    }
    pub fn store(&self, run_dir: &Path) -> Result<(), Error> {
        write_new(&run_dir.join("config.json"), &serde_json::to_vec_pretty(self)?)
    }
}

/// Viewing pace. `ZT_PACE_MS=N` (N > 0) makes every role pause N ms after
/// each durable journal record; unset, empty or 0 changes nothing. It changes
/// timing only, never a record or a decision.
pub const PACE_ENV: &str = "ZT_PACE_MS";
pub fn pace_ms() -> Result<u64, String> {
    match std::env::var(PACE_ENV) {
        Err(std::env::VarError::NotPresent) => Ok(0),
        Ok(text) if text.is_empty() => Ok(0),
        Ok(text) => text.parse().map_err(|_| format!("{PACE_ENV} must be a whole number of milliseconds, not {text:?}")),
        Err(_) => Err(format!("{PACE_ENV} must be a whole number of milliseconds")),
    }
}

/// Create a file that must not exist yet, then sync it.
pub fn write_new(path: &Path, bytes: &[u8]) -> Result<(), Error> {
    let mut f = fs::OpenOptions::new().write(true).create_new(true).open(path)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    Ok(())
}
pub fn write_new_json(path: &Path, value: &Value) -> Result<(), Error> {
    let mut bytes = serde_json::to_vec_pretty(value)?;
    bytes.push(b'\n');
    write_new(path, &bytes)
}
pub fn read_json(path: &Path) -> Result<Value, Error> {
    Ok(serde_json::from_slice(&fs::read(path)?)?)
}
