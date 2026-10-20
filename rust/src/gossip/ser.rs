//! The byte-exact serializer of `CLIENT_SPEC.md` §6.3.
//!
//! This does **not** go through `serde_json`'s pretty printer. The spec fixes a
//! key order that no generic printer produces, and it must not depend on whether
//! some other crate in the dependency graph happened to switch `serde_json` to
//! `preserve_order`. So the rules are written out here:
//!
//! 1. known keys first, in the fixed order;
//! 2. unknown keys after them, sorted by Unicode code point (which, for UTF-8,
//!    is byte order), recursively for nested objects; array order preserved;
//! 3. 2-space nesting-proportional indent, `": "` after a key, `","` between
//!    items;
//! 4. `[]` and `{}` inline when empty, expanded otherwise;
//! 5. exactly one trailing `\n`;
//! 6. literal UTF-8, RFC 8259 minimal escaping;
//! 7. integers with no decimal point or exponent.

use serde_json::{Map, Value};

/// §6.3(1) — the fixed key order for the six known keys.
pub(crate) const KNOWN_ORDER: [&str; 6] = [
    "chain",
    "root_node_ips",
    "try_new_peers",
    "reserved_peer_ips",
    "n_gossip_peers",
    "split_client_blocks",
];

/// Render a whole config object, trailing newline included.
pub(crate) fn render(map: &Map<String, Value>) -> String {
    let mut out = String::with_capacity(256);
    if map.is_empty() {
        out.push_str("{}");
    } else {
        let mut keys: Vec<&String> = Vec::with_capacity(map.len());
        for known in KNOWN_ORDER {
            if let Some((k, _)) = map.get_key_value(known) {
                keys.push(k);
            }
        }
        let mut unknown: Vec<&String> = map
            .keys()
            .filter(|k| !KNOWN_ORDER.contains(&k.as_str()))
            .collect();
        unknown.sort_unstable();
        keys.extend(unknown);

        out.push_str("{\n");
        for (i, k) in keys.iter().enumerate() {
            if i > 0 {
                out.push_str(",\n");
            }
            indent(&mut out, 1);
            escape_into(&mut out, k);
            out.push_str(": ");
            write_value(&mut out, &map[k.as_str()], 1);
        }
        out.push('\n');
        out.push('}');
    }
    out.push('\n');
    out
}

fn indent(out: &mut String, level: usize) {
    for _ in 0..level * 2 {
        out.push(' ');
    }
}

fn write_value(out: &mut String, v: &Value, level: usize) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => escape_into(out, s),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push_str("[\n");
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                indent(out, level + 1);
                write_value(out, item, level + 1);
            }
            out.push('\n');
            indent(out, level);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            // Nested objects sort by code point unconditionally (§6.3(2)).
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_unstable();
            out.push_str("{\n");
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                indent(out, level + 1);
                escape_into(out, k);
                out.push_str(": ");
                write_value(out, &map[k.as_str()], level + 1);
            }
            out.push('\n');
            indent(out, level);
            out.push('}');
        }
    }
}

/// RFC 8259 minimal escaping, matching the reference serializer
/// (`json.dumps(..., ensure_ascii=False)`): `"` and `\` plus the C0 controls,
/// with the five short forms. `/` is never escaped, and `DEL` is not a control
/// character for this purpose.
fn escape_into(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn obj(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn known_keys_lead_and_unknown_keys_sort() {
        let m = obj(json!({
            "zzz": 1,
            "try_new_peers": false,
            "aaa": {"b": 2, "a": 1},
            "chain": "Mainnet",
        }));
        assert_eq!(
            render(&m),
            "{\n  \"chain\": \"Mainnet\",\n  \"try_new_peers\": false,\n  \"aaa\": {\n    \"a\": 1,\n    \"b\": 2\n  },\n  \"zzz\": 1\n}\n"
        );
    }

    #[test]
    fn empty_containers_are_inline() {
        let m = obj(json!({"reserved_peer_ips": [], "x": {}}));
        assert_eq!(
            render(&m),
            "{\n  \"reserved_peer_ips\": [],\n  \"x\": {}\n}\n"
        );
    }

    #[test]
    fn non_ascii_is_literal_and_controls_are_escaped() {
        let m = obj(json!({"note": "Köln ✓\n\u{1}"}));
        assert_eq!(render(&m), "{\n  \"note\": \"Köln ✓\\n\\u0001\"\n}\n");
    }

    #[test]
    fn an_empty_object_is_just_braces() {
        assert_eq!(render(&Map::new()), "{}\n");
    }
}
