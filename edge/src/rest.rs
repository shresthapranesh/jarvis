//! The REST routes the edge serves itself, ahead of the proxy:
//! `GET /artifacts/{id}/raw` and `GET /documents/{id}/raw`
//! (`server/routes_artifacts.py`, `routes_documents.py`), `POST /uploads`
//! (`routes_uploads.py`) and the log viewer's `/server-logs` (`routes_logs.py`,
//! here `logs.rs`). Change both.
//!
//! A download answers as Starlette's `FileResponse` does: its headers, a
//! single byte range. (`HEAD` is FastAPI's 405, so Python's.) Whatever the edge doesn't reproduce — several ranges, a
//! range number Python's `int()` would read some other way, a path that isn't
//! a regular file — goes to Python untouched.

use std::path::{Path, PathBuf};

use axum::body::Body;
use axum::extract::{FromRequest, Multipart, Request};
use axum::http::{HeaderMap, Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};
use sqlx::SqlitePool;
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};

use crate::AppState;
use crate::pystr;

/// `_MAX_UPLOAD_BYTES`.
const MAX_UPLOAD_BYTES: u64 = 100 * 1024 * 1024;
/// `FileResponse.chunk_size`.
const CHUNK: usize = 64 * 1024;

/// The response, or the request back for the proxy.
pub async fn serve(state: &AppState, req: Request) -> Result<Response, Request> {
    let path = req.uri().path().to_string();
    // FastAPI's GET routes answer HEAD with a 405: Python's to give.
    if *req.method() == Method::GET {
        if let Some(id) = raw_id(&path, "/artifacts/") {
            return artifact(&state.pool, &id, req).await;
        }
        if let Some(id) = raw_id(&path, "/documents/") {
            return document(&state.pool, &id, req).await;
        }
    }
    match (req.method().clone(), path.as_str()) {
        (Method::POST, "/uploads") => upload(&state.config.staging_dir, req).await,
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

async fn artifact(pool: &SqlitePool, id: &str, req: Request) -> Result<Response, Request> {
    let row: Option<(String, String, Option<String>, String)> =
        match sqlx::query_as("SELECT title, filename, mime_type, kind FROM artifacts WHERE id = ?").bind(id).fetch_optional(pool).await {
            Ok(row) => row,
            Err(_) => return Err(req),
        };
    let Some((title, filename, mime_type, kind)) = row else { return Ok(not_found("not found")) };
    let path = resolve(&filename);
    if tokio::fs::metadata(&path).await.is_err() {
        return Ok(not_found("file missing"));
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

async fn document(pool: &SqlitePool, id: &str, req: Request) -> Result<Response, Request> {
    let row: Option<(String, String, String)> =
        match sqlx::query_as("SELECT filename, mime_type, path FROM documents WHERE id = ?").bind(id).fetch_optional(pool).await {
            Ok(row) => row,
            Err(_) => return Err(req),
        };
    let Some((filename, mime_type, stored)) = row else { return Ok(not_found("not found")) };
    let path = resolve(&stored);
    if tokio::fs::metadata(&path).await.is_err() {
        return Ok(not_found("file missing"));
    }
    let media_type = if mime_type.is_empty() { "application/octet-stream".to_string() } else { mime_type };
    file_response(&path, &media_type, &filename, "attachment", req).await
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
    Whole,
    One(u64, u64),
    Reply(Response),
    /// Several, or a number only Python's `int()` reads: Python answers.
    Python,
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
            // `int()` takes signs, underscores, other digits — or skips it.
            return Ranges::Python;
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
        _ => Ranges::Python,
    }
}

async fn file_response(path: &Path, media_type: &str, filename: &str, disposition: &str, req: Request) -> Result<Response, Request> {
    let meta = match tokio::fs::metadata(path).await {
        Ok(m) if m.is_file() => m,
        // Python's `FileResponse` raises on it; let it.
        _ => return Err(req),
    };
    let st = stat(&meta);
    let (start, end, status) = match ranges(req.headers(), &st) {
        Ranges::Whole => (0, st.size, StatusCode::OK),
        Ranges::One(start, end) => (start, end, StatusCode::PARTIAL_CONTENT),
        Ranges::Reply(response) => return Ok(response),
        Ranges::Python => return Err(req),
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
    let Ok(mut file) = tokio::fs::File::open(path).await else { return Err(req) };
    if file.seek(std::io::SeekFrom::Start(start)).await.is_err() {
        return Err(req);
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
    Ok(out.body(body).expect("a file response"))
}

/// The `file` part as Starlette's form parser leaves it: the last one wins.
enum Part {
    File { tmp: PathBuf, filename: String, content_type: Option<String>, size: u64, over: bool },
    Text(String),
}

/// FastAPI's 422 for the `file` form field.
fn unprocessable(part: Option<&Part>) -> Response {
    let detail = match part {
        Some(Part::Text(input)) => json!({"type": "value_error", "loc": ["body", "file"],
            "msg": "Value error, Expected UploadFile, received: <class 'str'>", "input": input, "ctx": {"error": {}}}),
        _ => json!({"type": "missing", "loc": ["body", "file"], "msg": "Field required", "input": null}),
    };
    json_response(StatusCode::UNPROCESSABLE_ENTITY, &json!({"detail": [detail]}))
}

async fn discard(part: Option<Part>) {
    if let Some(Part::File { tmp, .. }) = part {
        let _ = tokio::fs::remove_file(tmp).await;
    }
}

/// `upload_file`: the bytes and a `.meta.json` beside them in the staging
/// directory, streamed rather than held. A body that isn't a form has no
/// `file` (Starlette reads it as an empty form); an urlencoded one is
/// Python's to parse.
async fn upload(staging: &Path, req: Request) -> Result<Response, Request> {
    let content_type =
        req.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("").to_ascii_lowercase();
    if content_type.starts_with("application/x-www-form-urlencoded") {
        return Err(req);
    }
    if !content_type.starts_with("multipart/form-data") {
        return Ok(unprocessable(None));
    }
    let Ok(mut form) = Multipart::from_request(req, &()).await else {
        return Ok(unprocessable(None));
    };
    if tokio::fs::create_dir_all(staging).await.is_err() {
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    }
    let mut chosen: Option<Part> = None;
    loop {
        let mut field = match form.next_field().await {
            Ok(Some(field)) => field,
            Ok(None) => break,
            // A body the parser gives up on: no `file` came through.
            Err(_) => {
                discard(chosen.take()).await;
                return Ok(unprocessable(None));
            }
        };
        if field.name() != Some("file") {
            continue;
        }
        let part = match field.file_name().map(str::to_string) {
            None => match field.text().await {
                Ok(text) => Part::Text(text),
                Err(_) => {
                    discard(chosen.take()).await;
                    return Ok(unprocessable(None));
                }
            },
            Some(filename) => {
                let content_type = field.content_type().map(str::to_string);
                let tmp = staging.join(format!(".{}.part", uuid::Uuid::new_v4()));
                let Ok(mut out) = tokio::fs::File::create(&tmp).await else {
                    return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
                };
                let (mut size, mut over) = (0u64, false);
                loop {
                    match field.chunk().await {
                        Ok(Some(chunk)) => {
                            size += chunk.len() as u64;
                            over |= size > MAX_UPLOAD_BYTES;
                            if !over && out.write_all(&chunk).await.is_err() {
                                let _ = tokio::fs::remove_file(&tmp).await;
                                return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
                            }
                        }
                        Ok(None) => break,
                        Err(_) => {
                            let _ = tokio::fs::remove_file(&tmp).await;
                            discard(chosen.take()).await;
                            return Ok(unprocessable(None));
                        }
                    }
                }
                let _ = out.flush().await;
                Part::File { tmp, filename, content_type, size, over }
            }
        };
        discard(chosen.replace(part)).await;
    }
    let Some(Part::File { tmp, filename, content_type, size, over }) = chosen else {
        return Ok(unprocessable(chosen.as_ref()));
    };
    if over {
        let _ = tokio::fs::remove_file(&tmp).await;
        return Ok(json_response(
            StatusCode::PAYLOAD_TOO_LARGE,
            &json!({"error": format!("upload exceeds {} MiB limit", MAX_UPLOAD_BYTES / (1024 * 1024))}),
        ));
    }
    let upload_id = uuid::Uuid::new_v4().to_string();
    let filename = if filename.is_empty() { upload_id.clone() } else { filename };
    let mime_type = content_type.filter(|c| !c.is_empty()).unwrap_or_else(|| "application/octet-stream".into());
    let meta = json!({
        "filename": filename,
        "mime_type": mime_type,
        "size": size,
        "created_at": isoformat_utc_now(),
    });
    let wrote = tokio::fs::rename(&tmp, staging.join(&upload_id)).await.is_ok()
        && tokio::fs::write(staging.join(format!("{upload_id}.meta.json")), crate::pyjson::dumps(&meta)).await.is_ok();
    if !wrote {
        let _ = tokio::fs::remove_file(&tmp).await;
        return Ok(StatusCode::INTERNAL_SERVER_ERROR.into_response());
    }
    Ok(json_response(
        StatusCode::OK,
        &json!({"uploadId": upload_id, "filename": filename, "mimeType": mime_type, "size": size}),
    ))
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
