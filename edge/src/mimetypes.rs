//! `mimetypes.guess_type(name)[0]`, as the Python server answers it: the
//! module's built-in tables (`mimetypes.json`, exported from Python's
//! `_types_map_default` and friends — re-export with `JARVIS_UPDATE_GOLDEN=1
//! uv run pytest tests/test_edge_loop.py -k mimetypes`), then each of
//! `mimetypes.knownfiles` that exists, read in order, later entries winning.
//! Diffed against Python in `tests/test_edge_loop.py`.

use std::collections::HashMap;
use std::sync::LazyLock;

/// `mimetypes.knownfiles`.
const KNOWN_FILES: &[&str] = &[
    "/etc/mime.types",
    "/etc/httpd/mime.types",
    "/etc/httpd/conf/mime.types",
    "/etc/apache/mime.types",
    "/etc/apache2/mime.types",
    "/usr/local/etc/httpd/conf/mime.types",
    "/usr/local/lib/netscape/mime.types",
    "/usr/local/etc/httpd/conf/mime.types",
    "/usr/local/etc/mime.types",
];

struct Db {
    types: HashMap<String, String>,
    suffixes: HashMap<String, String>,
    encodings: HashMap<String, String>,
}

static DB: LazyLock<Db> = LazyLock::new(|| {
    #[derive(serde::Deserialize)]
    struct Defaults {
        types: HashMap<String, String>,
        suffixes: HashMap<String, String>,
        encodings: HashMap<String, String>,
    }
    let d: Defaults = serde_json::from_str(include_str!("mimetypes.json")).expect("mimetypes.json parses");
    let mut types = d.types;
    for file in KNOWN_FILES {
        if let Ok(text) = std::fs::read_to_string(file) {
            read_mime_types(&text, &mut types);
        }
    }
    Db { types, suffixes: d.suffixes, encodings: d.encodings }
});

/// `MimeTypes.readfp`: `type ext ext …` per line, `#` to the end of a line.
fn read_mime_types(text: &str, types: &mut HashMap<String, String>) {
    for line in text.lines() {
        let words: Vec<&str> = line.split_whitespace().take_while(|w| !w.starts_with('#')).collect();
        if let Some((ty, exts)) = words.split_first() {
            for ext in exts {
                types.insert(format!(".{ext}"), ty.to_string());
            }
        }
    }
}

/// `posixpath.splitext`: the last dot after the last slash, unless every
/// character before it in the name is a dot too.
fn splitext(p: &str) -> (&str, &str) {
    let name_start = p.rfind('/').map_or(0, |i| i + 1);
    match p.rfind('.') {
        Some(dot) if dot >= name_start && p[name_start..dot].chars().any(|c| c != '.') => (&p[..dot], &p[dot..]),
        _ => (p, ""),
    }
}

/// The path part of `name` as `guess_type` reads it: a name with a URL
/// scheme of two or more letters (`urlsplit`'s rule) loses the scheme, any
/// `//netloc`, and a query or fragment; anything else is a file path.
fn url_path(name: &str) -> Option<&str> {
    let colon = name.find(':')?;
    let scheme = &name[..colon];
    let valid = scheme.len() > 1
        && scheme.starts_with(|c: char| c.is_ascii_alphabetic())
        && scheme.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c));
    if !valid {
        return None;
    }
    let mut rest = &name[colon + 1..];
    if let Some(after) = rest.strip_prefix("//") {
        rest = after.find(['/', '?', '#']).map_or("", |i| &after[i..]);
    }
    Some(rest.split(['#', '?']).next().unwrap_or(""))
}

/// `mimetypes.guess_type(name)[0]`.
pub fn guess_type(name: &str) -> Option<String> {
    let path = match url_path(name) {
        // `data:` URLs carry their own type.
        Some(rest) if name[..name.find(':').unwrap_or(0)].eq_ignore_ascii_case("data") => {
            let comma = rest.find(',')?;
            let ty = &rest[..rest[..comma].find(';').unwrap_or(comma)];
            return Some(if ty.contains('=') || !ty.contains('/') { "text/plain".into() } else { ty.into() });
        }
        Some(rest) => rest,
        None => name,
    };
    let db = &*DB;
    let (b, e) = splitext(path);
    let (mut base, mut ext) = (b.to_string(), e.to_string());
    while let Some(replacement) = db.suffixes.get(&ext.to_lowercase()) {
        let joined = format!("{base}{replacement}");
        let (b, e) = splitext(&joined);
        (base, ext) = (b.to_string(), e.to_string());
    }
    // Case-sensitive, unlike the rest.
    if db.encodings.contains_key(&ext) {
        ext = splitext(&base).1.to_string();
    }
    db.types.get(&ext.to_lowercase()).cloned()
}

/// `core/artifact_storage.py:infer_kind`.
pub fn infer_kind(mime_type: Option<&str>, ext: &str) -> &'static str {
    if let Some(prefix) = mime_type.map(|m| m.split('/').next().unwrap_or("")) {
        match prefix {
            "audio" => return "audio",
            "video" => return "video",
            "image" => return "image",
            _ => {}
        }
    }
    match ext.to_lowercase().trim_start_matches('.') {
        "mp3" | "wav" | "ogg" | "m4a" | "flac" | "aac" => "audio",
        "mp4" | "webm" | "mov" | "mkv" | "avi" => "video",
        "png" | "jpg" | "jpeg" | "gif" | "webp" | "svg" | "bmp" => "image",
        _ => "binary",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitext_is_posixpaths() {
        assert_eq!(splitext("a.tar.gz"), ("a.tar", ".gz"));
        assert_eq!(splitext(".bashrc"), (".bashrc", ""));
        assert_eq!(splitext("..x"), ("..x", ""));
        assert_eq!(splitext("a."), ("a", "."));
        assert_eq!(splitext("d.x/noext"), ("d.x/noext", ""));
    }

    #[test]
    fn common_names() {
        assert_eq!(guess_type("song.MP3").as_deref(), Some("audio/mpeg"));
        assert_eq!(guess_type("a.tgz").as_deref(), Some("application/x-tar"));
        assert_eq!(guess_type("noext"), None);
        assert_eq!(infer_kind(None, ".PNG"), "image");
        assert_eq!(infer_kind(Some("application/pdf"), ".pdf"), "binary");
    }
}
