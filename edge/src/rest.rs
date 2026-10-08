//! The REST routes: `GET /artifacts/{id}/raw` (a port of
//! `server/routes_artifacts.py`) and the log viewer's `/server-logs` (of
//! `routes_logs.py`, here `logs.rs`).
//!
//! A download answers as Starlette's `FileResponse` did: its headers, a
//! single byte range. `HEAD` is a 405, as FastAPI's GET routes answer it.
//! Several ranges, or a range number Python's `int()` would have read some
//! other way, get the whole file — a server may ignore `Range` — and a path
//! that isn't a regular file is missing.

use std::path::{Path, PathBuf};

use axum::body::Body;
use axum::extract::Request;
use axum::http::{HeaderMap, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};
use sqlx::SqlitePool;
use tokio::io::{AsyncReadExt, AsyncSeekExt};

use crate::AppState;
use crate::pystr;

/// `FileResponse.chunk_size`.
const CHUNK: usize = 64 * 1024;

/// The response, or the request back when it is for no route here.
pub async fn serve(state: &AppState, req: Request) -> Result<Response, Request> {
    let path = req.uri().path().to_string();
    if let Some(id) = raw_id(&path, "/artifacts/") {
        return Ok(match *req.method() {
            Method::GET => artifact(&state.pool, &id, req).await,
            _ => json_response(StatusCode::METHOD_NOT_ALLOWED, &json!({"detail": "Method Not Allowed"})),
        });
    }
    match (req.method().clone(), path.as_str()) {
        (Method::GET, "/server-logs") => Ok(crate::logs::list(req.headers())),
        (Method::GET, "/server-logs/stream") => Ok(crate::logs::stream(req.headers())),
        _ => Err(req),
    }
}

/// `{prefix}{id}/raw`, the id percent-decoded as Starlette decodes a path.
fn raw_id(path: &str, prefix: &str) -> Option<String> {
    let id = path.strip_prefix(prefix)?.strip_suffix("/raw")?;
    if id.is_empty() || id.contains('/') {
        return None;
    }
    percent_encoding::percent_decode_str(id).decode_utf8().ok().map(|s| s.into_owned())
}

/// `JSONResponse` — compact, non-ASCII kept.
fn json_response(status: StatusCode, body: &Value) -> Response {
    (status, [(header::CONTENT_TYPE, "application/json")], body.to_string()).into_response()
}

fn not_found(what: &str) -> Response {
    json_response(StatusCode::NOT_FOUND, &json!({"error": what}))
}

/// A stored path, as Python's working directory (the checkout) reads it.
fn resolve(stored: &str) -> PathBuf {
    let path = Path::new(stored);
    if path.is_absolute() { path.to_path_buf() } else { crate::config::app_dir().join(path) }
}

async fn artifact(pool: &SqlitePool, id: &str, req: Request) -> Response {
    let row: Option<(String, String, Option<String>, String)> =
        match sqlx::query_as("SELECT title, filename, mime_type, kind FROM artifacts WHERE id = ?").bind(id).fetch_optional(pool).await {
            Ok(row) => row,
            Err(e) => return json_response(StatusCode::INTERNAL_SERVER_ERROR, &json!({"error": e.to_string()})),
        };
    let Some((title, filename, mime_type, kind)) = row else { return not_found("not found") };
    let path = resolve(&filename);
    if !tokio::fs::metadata(&path).await.is_ok_and(|m| m.is_file()) {
        return not_found("file missing");
    }
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let media_type = mime_type
        .filter(|m| !m.is_empty())
        .or_else(|| crate::mimetypes::guess_type(&name))
        .unwrap_or_else(|| "application/octet-stream".into());
    let title = if title.is_empty() { id } else { title.as_str() };
    let inline = ["audio", "video", "image"].contains(&kind.as_str());
    let download = format!("{title}{}", suffix(&name));
    file_response(&path, &media_type, &download, if inline { "inline" } else { "attachment" }, req).await
}

/// `PurePath.suffix`: the last `.ext` of the name, unless the dot leads or
/// ends it.
fn suffix(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[i..],
        _ => "",
    }
}

/// `urllib.parse.quote(s)`: everything but `A-Za-z0-9_.-~/` percent-encoded.
fn quote(s: &str) -> String {
    use std::fmt::Write;
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~/".contains(&b) {
            out.push(b as char);
        } else {
            let _ = write!(out, "%{b:02X}");
        }
    }
    out
}

/// What `FileResponse` sets from the name and the file.
struct Stat {
    size: u64,
    last_modified: String,
    etag: String,
}

fn stat(meta: &std::fs::Metadata) -> Stat {
    use std::os::unix::fs::MetadataExt;
    // `os.stat().st_mtime`: seconds plus nanoseconds as one double.
    let mtime = meta.mtime() as f64 + meta.mtime_nsec() as f64 * 1e-9;
    let at = chrono::DateTime::from_timestamp(meta.mtime(), 0).unwrap_or_default();
    let base = format!("{}-{}", crate::pyjson::float_repr(mtime), meta.len());
    Stat {
        size: meta.len(),
        last_modified: at.format("%a, %d %b %Y %H:%M:%S GMT").to_string(),
        etag: format!("\"{}\"", hex::encode(<md5::Md5 as md5::Digest>::digest(base.as_bytes()))),
    }
}

enum Ranges {
    /// No range, or one this serves whole: several, or a number only
    /// Python's `int()` would have read.
    Whole,
    One(u64, u64),
    Reply(Response),
}

fn plain(status: StatusCode, text: &str) -> Response {
    (status, [(header::CONTENT_TYPE, "text/plain; charset=utf-8")], text.to_string()).into_response()
}

/// `_parse_range_header` + `_parse_ranges`, for one range.
fn ranges(headers: &HeaderMap, st: &Stat) -> Ranges {
    let Some(range) = headers.get(header::RANGE).and_then(|v| v.to_str().ok()) else { return Ranges::Whole };
    if let Some(if_range) = headers.get(header::IF_RANGE) {
        let if_range = if_range.to_str().unwrap_or("");
        if if_range != st.last_modified && if_range != st.etag {
            return Ranges::Whole;
        }
    }
    let Some((units, spec)) = range.split_once('=') else { return Ranges::Reply(plain(StatusCode::BAD_REQUEST, "Malformed range header.")) };
    if pystr::strip(units).to_lowercase() != "bytes" {
        return Ranges::Reply(plain(StatusCode::BAD_REQUEST, "Only support bytes range"));
    }
    if spec.matches(',').count() + 1 > 100 {
        return Ranges::Whole;
    }
    let size = st.size;
    let number = |s: &str| -> Option<u64> { (!s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())).then(|| s.parse().ok()).flatten() };
    let mut out = vec![];
    for part in spec.split(',') {
        let part = pystr::strip(part);
        if part.is_empty() || part == "-" {
            continue;
        }
        let Some((start_s, end_s)) = part.split_once('-') else { continue };
        let (start_s, end_s) = (pystr::strip(start_s), pystr::strip(end_s));
        let plain_digits = |s: &str| s.is_empty() || number(s).is_some();
        if !plain_digits(start_s) || !plain_digits(end_s) {
            // `int()` takes signs, underscores, other digits: the whole file.
            return Ranges::Whole;
        }
        let start = match number(start_s) {
            Some(s) => s,
            None => size.saturating_sub(number(end_s).unwrap_or(0)),
        };
        let end = match (start_s.is_empty(), number(end_s)) {
            (false, Some(e)) if e < size => e + 1,
            _ => size,
        };
        out.push((start, end));
    }
    if out.is_empty() {
        return Ranges::Reply(plain(StatusCode::BAD_REQUEST, "Range header: range must be requested"));
    }
    if out.iter().any(|&(start, _)| start >= size) {
        return Ranges::Reply(
            Response::builder()
                .status(StatusCode::RANGE_NOT_SATISFIABLE)
                .header("content-range", format!("bytes */{size}"))
                .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
                .header(header::CONTENT_LENGTH, 0)
                .body(Body::empty())
                .expect("a 416"),
        );
    }
    if out.iter().any(|&(start, end)| start >= end) {
        return Ranges::Reply(plain(StatusCode::BAD_REQUEST, "Range header: start must be less than end"));
    }
    match out.as_slice() {
        [(start, end)] => Ranges::One(*start, *end),
        _ => Ranges::Whole,
    }
}

async fn file_response(path: &Path, media_type: &str, filename: &str, disposition: &str, req: Request) -> Response {
    let meta = match tokio::fs::metadata(path).await {
        Ok(m) if m.is_file() => m,
        _ => return not_found("file missing"),
    };
    let st = stat(&meta);
    let (start, end, status) = match ranges(req.headers(), &st) {
        Ranges::Whole => (0, st.size, StatusCode::OK),
        Ranges::One(start, end) => (start, end, StatusCode::PARTIAL_CONTENT),
        Ranges::Reply(response) => return response,
    };
    let quoted = quote(filename);
    let content_disposition = if quoted != filename {
        format!("{disposition}; filename*=utf-8''{quoted}")
    } else {
        format!("{disposition}; filename=\"{filename}\"")
    };
    let content_type = if media_type.starts_with("text/") && !media_type.to_lowercase().contains("charset=") {
        format!("{media_type}; charset=utf-8")
    } else {
        media_type.to_string()
    };
    let mut out = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, content_type)
        .header(header::ACCEPT_RANGES, "bytes")
        .header(header::CONTENT_DISPOSITION, content_disposition)
        .header(header::CONTENT_LENGTH, end - start)
        .header(header::LAST_MODIFIED, &st.last_modified)
        .header(header::ETAG, &st.etag);
    if status == StatusCode::PARTIAL_CONTENT {
        out = out.header(header::CONTENT_RANGE, format!("bytes {start}-{}/{}", end - 1, st.size));
    }
    let Ok(mut file) = tokio::fs::File::open(path).await else { return not_found("file missing") };
    if file.seek(std::io::SeekFrom::Start(start)).await.is_err() {
        return not_found("file missing");
    }
    let reader = file.take(end - start);
    let body = Body::from_stream(futures_util::stream::unfold(reader, |mut reader| async move {
        let mut buf = vec![0u8; CHUNK];
        match reader.read(&mut buf).await {
            Ok(0) => None,
            Ok(n) => {
                buf.truncate(n);
                Some((Ok::<_, std::io::Error>(bytes::Bytes::from(buf)), reader))
            }
            Err(e) => Some((Err(e), reader)),
        }
    }));
    out.body(body).expect("a file response")
}

/// `datetime.now(timezone.utc).isoformat()`.
pub(crate) fn isoformat_utc_now() -> String {
    let now = chrono::Utc::now();
    let micros = now.timestamp_subsec_micros();
    if micros == 0 {
        now.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        format!("{}.{micros:06}+00:00", now.format("%Y-%m-%dT%H:%M:%S"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quote_and_suffix_match_python() {
        assert_eq!(quote("report.pdf"), "report.pdf");
        assert_eq!(quote("a b/ü\"~"), "a%20b/%C3%BC%22~");
        assert_eq!(suffix("a.tar.gz"), ".gz");
        assert_eq!(suffix(".bashrc"), "");
        assert_eq!(suffix("a."), "");
        assert_eq!(suffix("noext"), "");
    }
}
