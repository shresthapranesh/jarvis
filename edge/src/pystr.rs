//! Python's `str` methods where Rust's differ: what counts as whitespace,
//! where lines break, and lengths and slices counted in code points.

/// `str.isspace()` for one character: Unicode whitespace, plus the
/// separators `\x1c`–`\x1f` Python counts and Rust doesn't.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// `s.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `len(s)`.
pub fn len(s: &str) -> usize {
    s.chars().count()
}

/// `s[:n]`.
pub fn prefix(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((at, _)) => &s[..at],
        None => s,
    }
}

/// `s.splitlines()`: every line boundary Python knows, `\r\n` as one, and
/// no empty line after a trailing break.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut lines = vec![];
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((at, c)) = chars.next() {
        if !matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            continue;
        }
        lines.push(&s[start..at]);
        start = at + c.len_utf8();
        if c == '\r' && chars.peek().is_some_and(|&(_, n)| n == '\n') {
            chars.next();
            start += 1;
        }
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_break_where_pythons_do() {
        assert_eq!(splitlines(""), Vec::<&str>::new());
        assert_eq!(splitlines("a\n"), ["a"]);
        assert_eq!(splitlines("a\r\nb\rc\x0bd\u{2028}e\n\nf"), ["a", "b", "c", "d", "e", "", "f"]);
        assert_eq!(strip("\x1c a \u{3000}"), "a");
        assert_eq!(prefix("héllo", 2), "hé");
        assert_eq!(prefix("hé", 9), "hé");
    }
}
