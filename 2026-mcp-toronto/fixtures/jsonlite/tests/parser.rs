use jsonlite::{parse, ParseError, Value};

fn s(text: &str) -> Value {
    Value::String(text.to_string())
}

#[test]
fn parses_literals() {
    assert_eq!(parse("null"), Ok(Value::Null));
    assert_eq!(parse("true"), Ok(Value::Bool(true)));
    assert_eq!(parse("false"), Ok(Value::Bool(false)));
    assert_eq!(parse("nul"), Err(ParseError::UnexpectedEnd));
    assert_eq!(parse("trux"), Err(ParseError::UnexpectedChar { pos: 3, found: 'x' }));
}

#[test]
fn parses_integers() {
    assert_eq!(parse("0"), Ok(Value::Number(0.0)));
    assert_eq!(parse("42"), Ok(Value::Number(42.0)));
    assert_eq!(parse("-17"), Ok(Value::Number(-17.0)));
    assert_eq!(parse("9007199254740991"), Ok(Value::Number(9007199254740991.0)));
}

#[test]
fn parses_floats_and_exponents() {
    assert_eq!(parse("3.25"), Ok(Value::Number(3.25)));
    assert_eq!(parse("-0.5"), Ok(Value::Number(-0.5)));
    assert_eq!(parse("1e3"), Ok(Value::Number(1000.0)));
    assert_eq!(parse("2.5E-2"), Ok(Value::Number(0.025)));
}

#[test]
fn rejects_invalid_numbers() {
    for input in ["-", "1.", "1e", "1e+", "-.5"] {
        assert_eq!(
            parse(input),
            Err(ParseError::InvalidNumber { pos: 0 }),
            "input: {input:?}"
        );
    }
    assert_eq!(parse("01"), Err(ParseError::TrailingCharacters { pos: 1 }));
}

#[test]
fn parses_string_escapes() {
    let input = r#""q\" b\\ s\/ \b\f\n\r\t""#;
    assert_eq!(parse(input), Ok(s("q\" b\\ s/ \u{8}\u{c}\n\r\t")));
    assert_eq!(parse(r#""plain text""#), Ok(s("plain text")));
}

#[test]
fn parses_unicode_escapes() {
    assert_eq!(parse(r#""Aé中""#), Ok(s("Aé中")));
    assert_eq!(parse("\"caf\u{e9}\""), Ok(s("café")));
}

#[test]
fn rejects_invalid_escapes() {
    assert_eq!(parse(r#""\x""#), Err(ParseError::InvalidEscape { pos: 1 }));
    assert_eq!(parse(r#""ab\u12G4""#), Err(ParseError::InvalidEscape { pos: 3 }));
    assert_eq!(parse(r#""\uD800""#), Err(ParseError::InvalidEscape { pos: 1 }));
}

#[test]
fn parses_nested_arrays() {
    let expected = Value::Array(vec![
        Value::Number(1.0),
        Value::Array(vec![Value::Bool(true), Value::Null]),
        Value::Array(vec![]),
        s("x"),
    ]);
    assert_eq!(parse(r#"[1,[true,null],[],"x"]"#), Ok(expected));
}

#[test]
fn parses_objects_in_source_order() {
    let value = parse(r#"{"b":1,"a":{"a":[]},"c":"d"}"#).expect("valid object");
    let expected = Value::Object(vec![
        ("b".to_string(), Value::Number(1.0)),
        ("a".to_string(), Value::Object(vec![("a".to_string(), Value::Array(vec![]))])),
        ("c".to_string(), s("d")),
    ]);
    assert_eq!(value, expected);
    assert_eq!(value.get("c"), Some(&s("d")));
    assert_eq!(parse("{}"), Ok(Value::Object(vec![])));
}

#[test]
fn tolerates_whitespace() {
    let input = " \n\t{ \"k\" :\r\n [ 1 , 2 ] } \n";
    let expected = Value::Object(vec![(
        "k".to_string(),
        Value::Array(vec![Value::Number(1.0), Value::Number(2.0)]),
    )]);
    assert_eq!(parse(input), Ok(expected));
}

#[test]
fn reports_unexpected_end() {
    for input in ["", "[1, 2", "{\"a\":", "\"open", "{"] {
        assert_eq!(parse(input), Err(ParseError::UnexpectedEnd), "input: {input:?}");
    }
}

#[test]
fn reports_unexpected_char() {
    assert_eq!(parse("[1 2]"), Err(ParseError::UnexpectedChar { pos: 3, found: '2' }));
    assert_eq!(parse("{1:2}"), Err(ParseError::UnexpectedChar { pos: 1, found: '1' }));
    assert_eq!(parse("[1,]"), Err(ParseError::UnexpectedChar { pos: 3, found: ']' }));
}

#[test]
fn rejects_trailing_characters() {
    assert_eq!(parse("true false"), Err(ParseError::TrailingCharacters { pos: 5 }));
    assert_eq!(parse("{}}"), Err(ParseError::TrailingCharacters { pos: 2 }));
}

#[test]
fn enforces_max_depth() {
    let ok = format!("{}{}", "[".repeat(64), "]".repeat(64));
    assert!(parse(&ok).is_ok(), "64 levels of nesting must be accepted");
    let too_deep = format!("{}{}", "[".repeat(65), "]".repeat(65));
    assert_eq!(parse(&too_deep), Err(ParseError::TooDeep { pos: 64 }));
    let err = parse(&too_deep).unwrap_err();
    assert_eq!(err.to_string(), "nesting exceeds 64 levels at byte 64");
}

#[test]
fn repeated_member_is_rejected() {
    let input = r#"{"name":"a","name":"b"}"#;
    let result = parse(input);
    assert!(
        matches!(result, Err(ParseError::DuplicateKey { ref key, .. }) if key == "name"),
        "expected DuplicateKey {{ key: \"name\" }} for input {input}, got {result:?}"
    );
}
