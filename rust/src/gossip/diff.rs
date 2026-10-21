//! A dependency-free unified diff, 3 lines of context, `\n` line endings
//! (`CLIENT_SPEC.md` §6.6(4)).

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tag {
    Equal,
    Delete,
    Insert,
}

fn split_lines(s: &str) -> Vec<&str> {
    if s.is_empty() {
        return Vec::new();
    }
    let mut v: Vec<&str> = s.split('\n').collect();
    // A trailing newline produces a final empty element that is not a line.
    if s.ends_with('\n') {
        v.pop();
    }
    v
}

/// Longest-common-subsequence edit script. Inputs here are a few dozen lines,
/// so the quadratic table is free.
fn script<'a>(a: &[&'a str], b: &[&'a str]) -> Vec<(Tag, &'a str)> {
    let (n, m) = (a.len(), b.len());
    let mut lcs = vec![0usize; (n + 1) * (m + 1)];
    let idx = |i: usize, j: usize| i * (m + 1) + j;
    for i in (0..n).rev() {
        for j in (0..m).rev() {
            lcs[idx(i, j)] = if a[i] == b[j] {
                lcs[idx(i + 1, j + 1)] + 1
            } else {
                lcs[idx(i + 1, j)].max(lcs[idx(i, j + 1)])
            };
        }
    }
    let (mut i, mut j) = (0usize, 0usize);
    let mut out = Vec::new();
    while i < n && j < m {
        if a[i] == b[j] {
            out.push((Tag::Equal, a[i]));
            i += 1;
            j += 1;
        } else if lcs[idx(i + 1, j)] >= lcs[idx(i, j + 1)] {
            out.push((Tag::Delete, a[i]));
            i += 1;
        } else {
            out.push((Tag::Insert, b[j]));
            j += 1;
        }
    }
    while i < n {
        out.push((Tag::Delete, a[i]));
        i += 1;
    }
    while j < m {
        out.push((Tag::Insert, b[j]));
        j += 1;
    }
    out
}

/// Render a unified diff of `old` against `new`.
///
/// `from`/`to` name the two sides in the `---`/`+++` headers. An empty result
/// means the two texts are identical.
pub(crate) fn unified(old: &str, new: &str, from: &str, to: &str) -> String {
    const CONTEXT: usize = 3;
    if old == new {
        return String::new();
    }
    let a = split_lines(old);
    let b = split_lines(new);
    let ops = script(&a, &b);

    // Group the script into hunks around the changed regions.
    let changed: Vec<usize> = ops
        .iter()
        .enumerate()
        .filter(|(_, (t, _))| *t != Tag::Equal)
        .map(|(i, _)| i)
        .collect();
    if changed.is_empty() {
        return String::new();
    }

    let mut hunks: Vec<(usize, usize)> = Vec::new();
    for &c in &changed {
        let lo = c.saturating_sub(CONTEXT);
        let hi = (c + CONTEXT + 1).min(ops.len());
        match hunks.last_mut() {
            Some(last) if lo <= last.1 => last.1 = last.1.max(hi),
            _ => hunks.push((lo, hi)),
        }
    }

    // Line numbers as we walk the script.
    let mut a_no = vec![0usize; ops.len() + 1];
    let mut b_no = vec![0usize; ops.len() + 1];
    let (mut ai, mut bi) = (0usize, 0usize);
    for (k, (t, _)) in ops.iter().enumerate() {
        a_no[k] = ai;
        b_no[k] = bi;
        match t {
            Tag::Equal => {
                ai += 1;
                bi += 1;
            }
            Tag::Delete => ai += 1,
            Tag::Insert => bi += 1,
        }
    }
    a_no[ops.len()] = ai;
    b_no[ops.len()] = bi;

    let mut out = String::new();
    out.push_str(&format!("--- {from}\n"));
    out.push_str(&format!("+++ {to}\n"));
    for (lo, hi) in hunks {
        let a_len = ops[lo..hi]
            .iter()
            .filter(|(t, _)| *t != Tag::Insert)
            .count();
        let b_len = ops[lo..hi]
            .iter()
            .filter(|(t, _)| *t != Tag::Delete)
            .count();
        let a_start = if a_len == 0 { a_no[lo] } else { a_no[lo] + 1 };
        let b_start = if b_len == 0 { b_no[lo] } else { b_no[lo] + 1 };
        out.push_str(&format!("@@ -{a_start},{a_len} +{b_start},{b_len} @@\n"));
        for (t, line) in &ops[lo..hi] {
            match t {
                Tag::Equal => out.push(' '),
                Tag::Delete => out.push('-'),
                Tag::Insert => out.push('+'),
            }
            out.push_str(line);
            out.push('\n');
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identical_is_empty() {
        assert_eq!(unified("a\nb\n", "a\nb\n", "x", "y"), "");
    }

    #[test]
    fn a_single_insert() {
        let d = unified("a\nb\n", "a\nx\nb\n", "old", "new");
        assert!(d.starts_with("--- old\n+++ new\n@@ "), "{d}");
        assert!(d.contains("\n+x\n"), "{d}");
        assert!(d.contains("\n a\n"), "{d}");
    }

    #[test]
    fn against_an_empty_file() {
        let d = unified("", "a\n", "old", "new");
        assert!(d.contains("@@ -0,0 +1,1 @@\n+a\n"), "{d}");
    }
}
