//! "Is this line already said?" — a port of `tools/text_dedupe.py`, with the
//! part of `difflib.SequenceMatcher` it uses (`ratio()`, autojunk on, no
//! junk function). The SDK's `jarvis.project_memory(append)` answers the
//! same question in the kernel; a change to either is made in both.

use std::collections::HashMap;

use crate::pystr;

const DUP_RATIO: f64 = 0.85;
/// Below this, fuzzy matching is noise — only an exact hit counts.
const DUP_MIN_LEN: usize = 15;

/// `\w` in a `str` pattern.
fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// `normalize_entry`: no list marker, case or punctuation.
pub fn normalize_entry(line: &str) -> String {
    // re.sub(r"^\s*(?:[-*•+]|\d+[.)])\s*", "", line)
    let rest = line.trim_start_matches(pystr::is_space);
    let after_marker = match rest.strip_prefix(['-', '*', '•', '+']) {
        Some(after) => Some(after),
        None => {
            let digits = rest.trim_start_matches(|c: char| c.is_numeric());
            if digits.len() < rest.len() { digits.strip_prefix(['.', ')']) } else { None }
        }
    };
    let s = after_marker.map_or(line, |after| after.trim_start_matches(pystr::is_space));
    // re.sub(r"[^\w\s]+", " ", s.lower()), then the words.
    let lowered: String =
        s.to_lowercase().chars().map(|c| if is_word(c) || pystr::is_space(c) { c } else { ' ' }).collect();
    lowered.split(pystr::is_space).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ")
}

pub fn is_heading(line: &str) -> bool {
    let s = pystr::strip(line);
    s.starts_with('#') || (s.starts_with("**") && s.ends_with("**") && pystr::len(s) > 4)
}

fn is_duplicate(norm: &str, known: &[String]) -> bool {
    let n = pystr::len(norm);
    for k in known {
        if norm == k {
            return true;
        }
        let m = pystr::len(k);
        if n < DUP_MIN_LEN || m < DUP_MIN_LEN {
            continue;
        }
        if k.contains(norm) || norm.contains(k.as_str()) {
            return true;
        }
        // The ratio can't beat 2*min/(n+m): skip the comparison when the
        // lengths alone rule it out.
        if 2.0 * n.min(m) as f64 / ((n + m) as f64) < DUP_RATIO {
            continue;
        }
        let (a, b): (Vec<char>, Vec<char>) = (norm.chars().collect(), k.chars().collect());
        if ratio(&a, &b) >= DUP_RATIO {
            return true;
        }
    }
    false
}

/// `dedupe_against`: the lines of `content` not already in `existing` (or
/// earlier in `content`), and how many were dropped. Headings pass, unless
/// nothing is left under them.
pub fn dedupe_against(existing: &str, content: &str) -> (String, usize) {
    let mut known: Vec<String> =
        pystr::splitlines(existing).into_iter().map(normalize_entry).filter(|n| !n.is_empty()).collect();
    let mut kept: Vec<&str> = vec![];
    let mut dropped = 0;
    for line in pystr::splitlines(content) {
        let norm = normalize_entry(line);
        if norm.is_empty() || is_heading(line) {
            kept.push(line);
            continue;
        }
        if is_duplicate(&norm, &known) {
            dropped += 1;
            continue;
        }
        kept.push(line);
        known.push(norm);
    }
    let mut out: Vec<&str> = vec![];
    for (i, line) in kept.iter().enumerate() {
        if is_heading(line) {
            let body = kept[i + 1..].iter().take_while(|next| !is_heading(next)).find(|next| !pystr::strip(next).is_empty());
            if body.is_none() {
                continue;
            }
        }
        out.push(line);
    }
    (pystr::strip(&out.join("\n")).to_string(), dropped)
}

// ── difflib.SequenceMatcher ──────────────────────────────────────────────────

/// `SequenceMatcher(None, a, b).ratio()`.
fn ratio(a: &[char], b: &[char]) -> f64 {
    let total = a.len() + b.len();
    if total == 0 {
        return 1.0;
    }
    2.0 * matching_size(a, b) as f64 / total as f64
}

/// The summed sizes of `get_matching_blocks()`.
fn matching_size(a: &[char], b: &[char]) -> usize {
    // `__chain_b`: where each element of b is; with autojunk, an element
    // in more than 1% of a long b is "popular" and left out.
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (j, &c) in b.iter().enumerate() {
        b2j.entry(c).or_default().push(j);
    }
    if b.len() >= 200 {
        let ntest = b.len() / 100 + 1;
        b2j.retain(|_, js| js.len() <= ntest);
    }
    let mut size = 0;
    let mut queue = vec![(0, a.len(), 0, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest_match(a, b, &b2j, alo, ahi, blo, bhi);
        if k > 0 {
            size += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    size
}

/// `find_longest_match(alo, ahi, blo, bhi)`. With no junk, only the first
/// pair of extension loops can fire: they grow the match over popular
/// elements on either side.
fn longest_match(
    a: &[char],
    b: &[char],
    b2j: &HashMap<char, Vec<usize>>,
    alo: usize,
    ahi: usize,
    blo: usize,
    bhi: usize,
) -> (usize, usize, usize) {
    let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0);
    let mut j2len: HashMap<usize, usize> = HashMap::new();
    for (i, c) in a.iter().enumerate().take(ahi).skip(alo) {
        let mut next: HashMap<usize, usize> = HashMap::new();
        for &j in b2j.get(c).map(Vec::as_slice).unwrap_or_default() {
            if j < blo {
                continue;
            }
            if j >= bhi {
                break;
            }
            let k = if j == 0 { 0 } else { j2len.get(&(j - 1)).copied().unwrap_or(0) } + 1;
            next.insert(j, k);
            if k > bestsize {
                (besti, bestj, bestsize) = (i + 1 - k, j + 1 - k, k);
            }
        }
        j2len = next;
    }
    while besti > alo && bestj > blo && a[besti - 1] == b[bestj - 1] {
        (besti, bestj, bestsize) = (besti - 1, bestj - 1, bestsize + 1);
    }
    while besti + bestsize < ahi && bestj + bestsize < bhi && a[besti + bestsize] == b[bestj + bestsize] {
        bestsize += 1;
    }
    (besti, bestj, bestsize)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r(a: &str, b: &str) -> f64 {
        ratio(&a.chars().collect::<Vec<_>>(), &b.chars().collect::<Vec<_>>())
    }

    #[test]
    fn ratios_are_difflibs() {
        // Values from CPython's difflib.
        assert_eq!(r("", ""), 1.0);
        assert_eq!(r("abcd", "bcde"), 0.75);
        assert_eq!(r("the edge serves graphql", "the edge serves the graphql api"), 0.8518518518518519);
        assert_eq!(r("private thread", "private volatile thread"), 0.7567567567567568);
        // Past 200 characters autojunk drops b's popular elements — here
        // the space and most letters — which is what sinks these ratios.
        let long = [
            (
                "axum serves and tokio the edge notes with edge axum rust the with graphql the edge and and edge graphql edge with and the notes rust edge graphql tokio tokio rust the rust rust and the graphql the with notes",
                "axum serves and tokio the edge notes with edge and rust with with graphql the edge and and serves rust edge with and the notes rust edge graphql tokio tokio rust the rust rust and the graphql edge with notes",
                0.4927536231884058,
            ),
            (
                "rust tokio graphql axum edge with memory edge rust the rust graphql sqlite tokio with and project axum sqlite rust sqlite axum over graphql project serves memory project graphql edge rust over with sqlite axum memory sqlite over rust edge edge with and serves project axum serves sqlite and the tokio edge project with rust project notes axum axum memory",
                "rust tokio graphql axum tokio with memory edge rust the rust graphql sqlite tokio with and project edge sqlite memory sqlite axum over graphql project serves memory project graphql project rust over with sqlite axum memory tokio sqlite axum edge edge with and serves sqlite axum the sqlite and the tokio edge project edge rust project notes axum axum memory",
                0.5654008438818565,
            ),
            (
                "tokio notes sqlite over memory and tokio axum the sqlite axum serves rust edge sqlite the graphql project over serves memory graphql and and notes sqlite edge serves sqlite and with over serves notes and notes with over memory and axum tokio and graphql serves edge serves serves graphql tokio graphql the sqlite notes rust serves over over the serves and with axum rust rust axum serves memory notes with rust tokio tokio memory the sqlite notes project notes tokio project with and and and and edge sqlite tokio and",
                "edge notes sqlite over memory and rust axum the the axum serves with edge sqlite the graphql project over rust sqlite graphql and and the sqlite notes serves sqlite and with over serves notes and notes with over memory and axum tokio and edge over edge rust serves rust tokio graphql the sqlite notes rust serves over over the serves and with axum rust rust axum serves memory notes with rust tokio tokio memory the sqlite notes project axum tokio project serves and and and and edge sqlite tokio and",
                0.08849557522123894,
            ),
        ];
        for (a, b, expected) in long {
            assert_eq!(r(a, b), expected, "{} chars", b.len());
        }
    }

    #[test]
    fn entries_normalize_as_pythons_do() {
        assert_eq!(normalize_entry("  - Uses **Rust** (1.80)!"), "uses rust 1 80");
        assert_eq!(normalize_entry("12) Step one"), "step one");
        assert_eq!(normalize_entry("12 apples"), "12 apples");
        assert_eq!(normalize_entry("•Café_au-lait"), "café_au lait");
    }

    #[test]
    fn headings_with_nothing_left_under_them_go() {
        let existing = "- The edge is written in Rust and serves GraphQL\n- Tests use pytest";
        let content = "## Stack\n- The edge is written in Rust, and serves GraphQL.\n## Tests\n- Tests use pytest\n- CI runs on every push";
        assert_eq!(dedupe_against(existing, content), ("## Tests\n- CI runs on every push".into(), 2));
    }
}
