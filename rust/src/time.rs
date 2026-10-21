//! The little bit of calendar arithmetic this crate needs, so that a `.bak`
//! name and an RFC 9110 `Retry-After` date need no date-time dependency.
//!
//! Howard Hinnant's civil-from-days / days-from-civil, which are exact for
//! every proleptic-Gregorian date.

/// Days since 1970-01-01 for a civil date.
pub(crate) fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// `(year, month, day, hour, minute, second)` for a Unix timestamp, UTC.
fn civil_from_unix(unix_secs: i64) -> (i64, i64, i64, i64, i64, i64) {
    let days = unix_secs.div_euclid(86_400);
    let rem = unix_secs.rem_euclid(86_400);
    let (hh, mm, ss) = (rem / 3600, (rem % 3600) / 60, rem % 60);

    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d, hh, mm, ss)
}

/// `%Y%m%dT%H%M%SZ` for a Unix timestamp.
pub(crate) fn format_utc(unix_secs: i64) -> String {
    let (y, m, d, hh, mm, ss) = civil_from_unix(unix_secs);
    format!("{y:04}{m:02}{d:02}T{hh:02}{mm:02}{ss:02}Z")
}

/// RFC 3339 `YYYY-MM-DDTHH:MM:SSZ` for a Unix timestamp.
pub(crate) fn format_rfc3339(unix_secs: i64) -> String {
    let (y, m, d, hh, mm, ss) = civil_from_unix(unix_secs);
    format!("{y:04}-{m:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}Z")
}

const MONTHS: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/// Parse an RFC 9110 IMF-fixdate — `Mon, 21 Sep 2026 02:36:00 GMT` — to a Unix
/// timestamp.
///
/// Only the fixed-width form is accepted, which is the one a conformant server
/// must send. Anything else is [`None`] and the caller treats the header as
/// absent (§5.2(4)): do not guess, and do not fail the request over it.
pub(crate) fn parse_imf_fixdate(s: &str) -> Option<i64> {
    let s = s.trim();
    // "Mon, 21 Sep 2026 02:36:00 GMT"
    let rest = s.split_once(", ")?.1;
    let mut parts = rest.split(' ');
    let day: i64 = parts.next()?.parse().ok()?;
    let month = parts.next()?;
    let year: i64 = parts.next()?.parse().ok()?;
    let time = parts.next()?;
    if parts.next()? != "GMT" || parts.next().is_some() {
        return None;
    }
    let month = MONTHS.iter().position(|m| *m == month)? as i64 + 1;
    if !(1..=31).contains(&day) {
        return None;
    }
    let mut hms = time.split(':');
    let h: i64 = hms.next()?.parse().ok()?;
    let mi: i64 = hms.next()?.parse().ok()?;
    let sec: i64 = hms.next()?.parse().ok()?;
    if hms.next().is_some() || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    Some(days_from_civil(year, month, day) * 86_400 + h * 3600 + mi * 60 + sec)
}

/// Parse an RFC 3339 instant in the `…Z` form the fixtures use. Test support.
#[cfg(test)]
pub(crate) fn parse_rfc3339_z(s: &str) -> Option<i64> {
    let (date, time) = s.trim_end_matches('Z').split_once('T')?;
    let mut d = date.split('-');
    let y: i64 = d.next()?.parse().ok()?;
    let m: i64 = d.next()?.parse().ok()?;
    let day: i64 = d.next()?.parse().ok()?;
    let mut t = time.split(':');
    let h: i64 = t.next()?.parse().ok()?;
    let mi: i64 = t.next()?.parse().ok()?;
    let sec: f64 = t.next().unwrap_or("0").parse().ok()?;
    Some(days_from_civil(y, m, day) * 86_400 + h * 3600 + mi * 60 + sec as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_against_known_instants() {
        assert_eq!(format_utc(0), "19700101T000000Z");
        assert_eq!(format_utc(1_758_421_200), "20250921T022000Z");
        assert_eq!(days_from_civil(1970, 1, 1), 0);
        assert_eq!(format_rfc3339(1_758_421_200), "2025-09-21T02:20:00Z");
    }

    #[test]
    fn the_fixture_instants() {
        // fixtures/http/error_429_retry_after_http_date.json
        let now = parse_rfc3339_z("2026-09-21T02:35:00Z").unwrap();
        let after = parse_imf_fixdate("Mon, 21 Sep 2026 02:36:00 GMT").unwrap();
        assert_eq!(after - now, 60);
    }

    #[test]
    fn a_non_date_is_none() {
        assert_eq!(parse_imf_fixdate("soon-ish"), None);
        assert_eq!(parse_imf_fixdate("42"), None);
        assert_eq!(parse_imf_fixdate("Mon, 21 Foo 2026 02:36:00 GMT"), None);
        assert_eq!(parse_imf_fixdate("Mon, 21 Sep 2026 02:36:00 UTC"), None);
    }
}
