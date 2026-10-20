//! IPv4 shape checking, and the deliberate IPv6 refusal of D-B.

use std::net::Ipv4Addr;
use std::str::FromStr;

/// The verbatim reason `CLIENT_SPEC.md` §6.4(3) requires for an IPv6 literal.
pub(crate) const IPV6_REASON: &str = "IPv6 is out of contract: the peer API is IPv4-only and the node's support for an IPv6 literal here is unverified. Refusing rather than writing an untested value into a file whose failure mode is a crash loop.";

/// Is this a dotted-quad IPv4 literal?
///
/// Rust's own parser is strict: four decimal octets, no leading zeros, no
/// shorthand forms, no port.
pub(crate) fn is_ipv4(s: &str) -> bool {
    Ipv4Addr::from_str(s).is_ok()
}

/// Does this look like an IPv6 literal? Used only to pick the better message.
pub(crate) fn looks_like_ipv6(s: &str) -> bool {
    s.contains(':')
}

/// The §6.4(3) reason string for a value that is not an acceptable address.
pub(crate) fn bad_address_reason(value: &str) -> String {
    if looks_like_ipv6(value) {
        IPV6_REASON.to_string()
    } else {
        format!("{value} is not an IPv4 address")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strictness() {
        assert!(is_ipv4("192.0.2.10"));
        assert!(!is_ipv4("999.0.2.10"));
        assert!(!is_ipv4("192.0.2.10:8080"));
        assert!(!is_ipv4("2001:db8::1"));
        assert!(!is_ipv4(""));
        assert!(looks_like_ipv6("2001:db8::1"));
    }
}
