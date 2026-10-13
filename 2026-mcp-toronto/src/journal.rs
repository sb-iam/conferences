//! Per-role append-only journal. A durable record is written and synced
//! before its `Ack` exists, and only this module can construct an `Ack`, so a
//! protected handoff that demands one cannot be satisfied by JSON on disk.
//!
//! There are no restart generations: this demo has no restart scene.
use crate::util::{pace_ms, sha256_hex};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::Path,
    time::{Duration, Instant},
};

pub struct Journal {
    file: File,
    role: String,
    seq: u64,
    prev: String,
    start: Instant,
    failed: bool,
    /// Viewing pace: how long this role stands still after each synced record.
    pace: Duration,
}

/// Proof that one exact record reached the synced journal in this process.
/// Not `Clone`, and it has no public constructor.
#[derive(Debug)]
#[allow(dead_code)]
pub struct Ack {
    pub seq: u64,
    _private: (),
}

impl Journal {
    /// Create the role's journal. It must not exist yet: every run is fresh.
    pub fn create(dir: &Path, role: &str) -> io::Result<Self> {
        let pace = Duration::from_millis(pace_ms().map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?);
        fs::create_dir_all(dir)?;
        let path = dir.join("journal.ndjson");
        let file = OpenOptions::new().append(true).create_new(true).open(&path)?;
        file.sync_all()?;
        File::open(dir)?.sync_all()?;
        Ok(Self { file, role: role.to_owned(), seq: 0, prev: "genesis".to_owned(), start: Instant::now(), failed: false, pace })
    }
    fn write(&mut self, kind: &str, body: Value, durable: bool) -> io::Result<Ack> {
        // A writer that failed once never reports another record as durable.
        if self.failed {
            return Err(io::Error::other("journal writer failed earlier"));
        }
        self.seq += 1;
        let mut line = serde_json::to_vec(&json!({
            "seq": self.seq, "role": self.role, "kind": kind, "durable": durable,
            "t_ms": self.start.elapsed().as_millis() as u64, "prev": self.prev, "body": body,
        }))?;
        line.push(b'\n');
        let outcome = self.file.write_all(&line).and_then(|_| if durable { self.file.sync_all() } else { Ok(()) });
        if let Err(e) = outcome {
            self.failed = true;
            return Err(e);
        }
        self.prev = sha256_hex(&line);
        Ok(Ack { seq: self.seq, _private: () })
    }
    /// Append and sync. The returned `Ack` is the only evidence of consumption.
    pub fn durable(&mut self, kind: &str, body: Value) -> io::Result<Ack> {
        let ack = self.write(kind, body, true)?;
        // Viewing pace: the record is synced; stand still before going on.
        if !self.pace.is_zero() {
            std::thread::sleep(self.pace);
        }
        Ok(ack)
    }
    /// Append an observation that authorizes nothing.
    pub fn note(&mut self, kind: &str, body: Value) -> io::Result<()> {
        self.write(kind, body, false).map(|_| ())
    }
}
