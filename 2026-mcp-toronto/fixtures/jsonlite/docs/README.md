# jsonlite

A tiny, dependency-free JSON parser written as a hand-rolled recursive-descent
parser.

## Usage

```rust
use jsonlite::{parse, ParseError, Value};

let doc = parse(r#"{"name": "jsonlite", "tags": ["small", "strict"]}"#).unwrap();
assert_eq!(doc.get("name"), Some(&Value::String("jsonlite".into())));

match parse("[1, 2") {
    Err(ParseError::UnexpectedEnd) => eprintln!("input was truncated"),
    other => panic!("unexpected result: {other:?}"),
}
```

## Behaviour

- Supports `null`, booleans, numbers (as `f64`), strings, arrays and objects.
- String escapes: `\" \\ \/ \b \f \n \r \t` and `\uXXXX` (Basic Multilingual
  Plane only; surrogate pairs are rejected).
- Objects keep their members in source order.
- Duplicate object keys are rejected with `ParseError::DuplicateKey`.
- Nesting deeper than 64 arrays/objects is rejected with `ParseError::TooDeep`.
- Anything after the top-level value other than whitespace is rejected with
  `ParseError::TrailingCharacters`.

Error positions are byte offsets into the input.
