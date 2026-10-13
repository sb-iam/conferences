//! `jsonlite`: a tiny, dependency-free JSON parser.
//!
//! ```
//! let value = jsonlite::parse(r#"{"ok": true}"#).unwrap();
//! assert_eq!(value.get("ok"), Some(&jsonlite::Value::Bool(true)));
//! ```

pub mod parser;

pub use parser::{parse, ParseError, Value};
