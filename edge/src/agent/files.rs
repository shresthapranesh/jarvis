//! `read_file`, `write_file` and `list_files` — a port of `tools/files.py`
//! for the workers that are bound to them; a change to either is made in
//! both.
//!
//! A relative path is the Python server's: resolved against the app
//! directory, and echoed as `str(pathlib.Path(path))` spells it. A worker
//! runs without a memory store, so a `memory/` path only says so.

use std::io;
use std::path::{Path, PathBuf};

const MEMORY_PREFIX: &str = "memory/";
const NO_STORE: &str = "Memory store unavailable in this context.";

/// `write_file(filepath, content)`. An `Err` is what Python raises.
pub fn write_file(cwd: &Path, filepath: &str, content: &str) -> Result<String, String> {
    if filepath.starts_with(MEMORY_PREFIX) {
        return Ok(NO_STORE.into());
    }
    let path = resolve(cwd, filepath);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| os_error(&e, &py_path(parent_str(filepath))))?;
    }
    std::fs::write(&path, content).map_err(|e| os_error(&e, &py_path(filepath)))?;
    Ok(format!("Written to {}", py_path(filepath)))
}

/// `read_file(filepath)`.
pub fn read_file(cwd: &Path, filepath: &str) -> Result<String, String> {
    if filepath.starts_with(MEMORY_PREFIX) {
        return Ok(NO_STORE.into());
    }
    let path = resolve(cwd, filepath);
    if !path.exists() {
        return Ok(format!("File not found: {filepath}"));
    }
    read_text(&path, &py_path(filepath))
}

/// `list_files(directory)`: the files in it, by name.
pub fn list_files(cwd: &Path, directory: &str) -> Result<String, String> {
    let path = resolve(cwd, directory);
    if !path.exists() {
        return Ok(format!("Directory not found: {directory}"));
    }
    let shown = py_path(directory);
    let entries = std::fs::read_dir(&path).map_err(|e| os_error(&e, &shown))?;
    let mut names = vec![];
    for entry in entries {
        let entry = entry.map_err(|e| os_error(&e, &shown))?;
        // `is_file()` follows links, as Python's does.
        if entry.path().is_file() {
            names.push(entry.file_name().to_string_lossy().into_owned());
        }
    }
    if names.is_empty() {
        return Ok(format!("No files found in {directory}"));
    }
    names.sort();
    let joined: Vec<String> = names
        .iter()
        .map(|n| match shown.as_str() {
            "." => n.clone(),
            s if s.ends_with('/') => format!("{s}{n}"),
            s => format!("{s}/{n}"),
        })
        .collect();
    Ok(joined.join("\n"))
}

/// `Path.read_text(encoding="utf-8")`: universal newlines, as text mode reads.
pub fn read_text(path: &Path, shown: &str) -> Result<String, String> {
    let bytes = std::fs::read(path).map_err(|e| os_error(&e, shown))?;
    let text = String::from_utf8(bytes).map_err(|e| decode_error(e.utf8_error(), e.as_bytes()))?;
    Ok(text.replace("\r\n", "\n").replace('\r', "\n"))
}

fn resolve(cwd: &Path, path: &str) -> PathBuf {
    cwd.join(path)
}

/// What `Path(path).parent` is built from, before normalizing.
fn parent_str(path: &str) -> &str {
    let trimmed = path.trim_end_matches('/');
    match trimmed.rfind('/') {
        Some(0) => "/",
        Some(at) => &trimmed[..at],
        None => ".",
    }
}

/// `str(PurePosixPath(path))`: no empty or `.` parts, no trailing slash,
/// `.` for nothing; exactly two leading slashes are kept.
pub fn py_path(path: &str) -> String {
    let lead = if path.starts_with("//") && !path.starts_with("///") {
        "//"
    } else if path.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts: Vec<&str> = path.split('/').filter(|p| !p.is_empty() && *p != ".").collect();
    match (lead, parts.is_empty()) {
        ("", true) => ".".into(),
        (lead, _) => format!("{lead}{}", parts.join("/")),
    }
}

/// `str(OSError)`: `[Errno N] <strerror>: '<path>'`.
pub fn os_error(e: &io::Error, path: &str) -> String {
    match e.raw_os_error() {
        Some(code) => {
            let text = io::Error::from_raw_os_error(code).to_string();
            let text = text.split(" (os error").next().unwrap_or(&text).to_string();
            format!("[Errno {code}] {text}: {}", crate::pyjson::repr_str(path))
        }
        None => e.to_string(),
    }
}

/// `str(UnicodeDecodeError)`, for the usual cases.
fn decode_error(e: std::str::Utf8Error, bytes: &[u8]) -> String {
    let at = e.valid_up_to();
    let byte = bytes[at];
    let reason = match e.error_len() {
        None => "unexpected end of data",
        Some(_) if (0x80..0xc2).contains(&byte) || byte > 0xf4 => "invalid start byte",
        Some(_) => "invalid continuation byte",
    };
    format!("'utf-8' codec can't decode byte 0x{byte:02x} in position {at}: {reason}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_print_as_pathlib_prints_them() {
        for (raw, shown) in [
            ("notes/a.md", "notes/a.md"),
            ("./notes//a.md/", "notes/a.md"),
            ("", "."),
            (".", "."),
            ("/tmp/x", "/tmp/x"),
            ("//srv/x", "//srv/x"),
            ("///srv/x", "/srv/x"),
            ("a/../b", "a/../b"),
        ] {
            assert_eq!(py_path(raw), shown, "{raw:?}");
        }
        assert_eq!(parent_str("a/b/c.txt"), "a/b");
        assert_eq!(parent_str("c.txt"), ".");
    }

    #[test]
    fn files_round_trip_and_list() {
        let dir = std::env::temp_dir().join(format!("jarvis-files-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(write_file(&dir, "sub/x.txt", "one\r\ntwo").unwrap(), "Written to sub/x.txt");
        assert_eq!(read_file(&dir, "./sub/x.txt").unwrap(), "one\ntwo");
        assert_eq!(read_file(&dir, "sub/none").unwrap(), "File not found: sub/none");
        write_file(&dir, "sub/b.txt", "").unwrap();
        std::fs::create_dir_all(dir.join("sub/dir")).unwrap();
        assert_eq!(list_files(&dir, "sub/").unwrap(), "sub/b.txt\nsub/x.txt");
        assert_eq!(list_files(&dir, "sub/dir").unwrap(), "No files found in sub/dir");
        assert_eq!(list_files(&dir, "nope").unwrap(), "Directory not found: nope");
        assert_eq!(read_file(&dir, "sub").unwrap_err(), "[Errno 21] Is a directory: 'sub'");
        assert_eq!(read_file(&dir, "memory/x").unwrap(), NO_STORE);
        std::fs::write(dir.join("bad"), [b'a', 0xff]).unwrap();
        assert_eq!(read_file(&dir, "bad").unwrap_err(), "'utf-8' codec can't decode byte 0xff in position 1: invalid start byte");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
