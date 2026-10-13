//! `zt`: one coding task through a gateway, a workspace and a test runner,
//! with the whole plan checked before any business handler runs and every
//! hop's exact bytes checked before release.
//!
//!   zt run   --scene healthy|swap|gist|substitute --out FRESH_DIR --demo DIR
//!   zt serve --role G|A|B --run-dir DIR
//!
//! `run` is host H and prints the scene panel. `serve` is one MCP server role;
//! its stdout carries protocol frames only.
mod gate;
mod host;
mod journal;
mod plan;
mod roles;
mod util;

use std::{collections::BTreeMap, path::Path};

const USAGE: &str = "usage:
  zt run --scene healthy|swap|gist|substitute --out FRESH_DIR --demo DIR
  zt serve --role G|A|B --run-dir DIR";

fn options(args: &[String], allowed: &[&str]) -> Result<BTreeMap<String, String>, util::Error> {
    let mut found = BTreeMap::new();
    let mut rest = args.iter();
    while let Some(key) = rest.next() {
        if !allowed.contains(&key.as_str()) {
            return Err(format!("unsupported option {key}\n{USAGE}").into());
        }
        let value = rest.next().ok_or_else(|| format!("{key} needs a value\n{USAGE}"))?;
        if found.insert(key.clone(), value.clone()).is_some() {
            return Err(format!("duplicate option {key}").into());
        }
    }
    for key in allowed {
        if !found.contains_key(*key) {
            return Err(format!("{key} is required\n{USAGE}").into());
        }
    }
    Ok(found)
}

async fn dispatch(args: Vec<String>) -> Result<i32, util::Error> {
    match args.first().map(String::as_str) {
        Some("run") => {
            let o = options(&args[1..], &["--scene", "--out", "--demo"])?;
            host::run(&o["--scene"], Path::new(&o["--out"]), Path::new(&o["--demo"])).await
        }
        Some("serve") => {
            let o = options(&args[1..], &["--role", "--run-dir"])?;
            let role = match o["--role"].as_str() {
                "G" => 'G',
                "A" => 'A',
                "B" => 'B',
                _ => return Err(USAGE.into()),
            };
            roles::serve(role, Path::new(&o["--run-dir"])).await?;
            Ok(0)
        }
        _ => Err(USAGE.into()),
    }
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let code = match dispatch(std::env::args().skip(1).collect()).await {
        Ok(code) => code,
        Err(e) => {
            eprintln!("zt: {e}");
            2
        }
    };
    // Exit directly so a parked stdin reader cannot delay shutdown.
    std::process::exit(code);
}
