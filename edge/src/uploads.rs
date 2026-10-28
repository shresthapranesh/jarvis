//! Chat attachments. `POST /uploads` stages a file, `startTask` names it
//! (`attachmentUploads`) and the turn that sends it takes it.
//!
//! Every attachment is moved into its conversation's directory under
//! `config::uploads_dir()` and handed to the agent as a path, so `run_cell`
//! can open it whatever the model. An image small enough for every provider
//! also goes to the model as an image part, its bytes a `transcript_blobs`
//! row as the transcript keeps all media (`edge/transcript_format.md`). The
//! directory goes with the conversation (`gql::conversation::Teardown`).

use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use axum::Json;
use axum::extract::{FromRequest, Multipart, Request};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;
use tokio::io::AsyncWriteExt;

use crate::gql::codec::now_stored;
use crate::llm::Blobs;
use crate::llm::transcript::{Content, Media, Part, Typed};

pub const MAX_UPLOAD_BYTES: u64 = 100 * 1024 * 1024;
/// The largest image sent to the model as an image — Anthropic's 5 MB per
/// image is the tightest of the providers. A larger one is a file only.
const MAX_IMAGE_BYTES: u64 = 5 * 1024 * 1024;
/// The image types every provider the edge calls reads.
const IMAGE_TYPES: [&str; 4] = ["image/png", "image/jpeg", "image/gif", "image/webp"];
/// The most of a transcript the prompt carries — about 5k tokens, or half an
/// hour of speech; the whole one is a file beside the audio.
const MAX_TRANSCRIPT_CHARS: usize = 20_000;
/// A staged file no turn took within this long is swept on the next upload.
const STAGED_FOR: Duration = Duration::from_secs(60 * 60);

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"error": message}))).into_response()
}

/// `POST /uploads`: the form's `file`, streamed into the staging directory
/// with a `.meta.json` beside it. Answers `{uploadId, filename, mimeType,
/// size}`.
pub async fn upload(req: Request) -> Response {
    let staging = crate::config::staging_dir();
    let Ok(mut form) = Multipart::from_request(req, &()).await else {
        return error(StatusCode::BAD_REQUEST, "expected a multipart form with a `file` field");
    };
    if tokio::fs::create_dir_all(&staging).await.is_err() {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "the staging directory can't be created");
    }
    sweep(&staging).await;
    loop {
        let mut field = match form.next_field().await {
            Ok(Some(field)) => field,
            Ok(None) => return error(StatusCode::BAD_REQUEST, "no `file` field in the form"),
            Err(_) => return error(StatusCode::BAD_REQUEST, "the form couldn't be read"),
        };
        if field.name() != Some("file") {
            continue;
        }
        let filename = clean_name(field.file_name().unwrap_or_default());
        let content_type = field.content_type().map(str::to_string);
        let upload_id = uuid::Uuid::new_v4().to_string();
        let tmp = staging.join(format!(".{upload_id}.part"));
        let Ok(mut out) = tokio::fs::File::create(&tmp).await else {
            return error(StatusCode::INTERNAL_SERVER_ERROR, "the upload can't be written");
        };
        let mut size = 0u64;
        loop {
            match field.chunk().await {
                Ok(Some(chunk)) => {
                    size += chunk.len() as u64;
                    if size > MAX_UPLOAD_BYTES {
                        let _ = tokio::fs::remove_file(&tmp).await;
                        let limit = format!("upload exceeds {} MiB limit", MAX_UPLOAD_BYTES / (1024 * 1024));
                        return error(StatusCode::PAYLOAD_TOO_LARGE, &limit);
                    }
                    if out.write_all(&chunk).await.is_err() {
                        let _ = tokio::fs::remove_file(&tmp).await;
                        return error(StatusCode::INTERNAL_SERVER_ERROR, "the upload can't be written");
                    }
                }
                Ok(None) => break,
                Err(_) => {
                    let _ = tokio::fs::remove_file(&tmp).await;
                    return error(StatusCode::BAD_REQUEST, "the upload was cut off");
                }
            }
        }
        let _ = out.flush().await;
        drop(out);
        // A browser says `application/octet-stream` for a type it doesn't know;
        // the name may know better.
        let mime_type = content_type
            .filter(|c| !c.is_empty() && c != "application/octet-stream")
            .or_else(|| crate::mimetypes::guess_type(&filename))
            .unwrap_or_else(|| "application/octet-stream".into());
        let meta = json!({"filename": filename, "mime_type": mime_type, "size": size});
        let wrote = tokio::fs::rename(&tmp, staging.join(&upload_id)).await.is_ok()
            && tokio::fs::write(staging.join(format!("{upload_id}.meta.json")), meta.to_string()).await.is_ok();
        if !wrote {
            let _ = tokio::fs::remove_file(&tmp).await;
            return error(StatusCode::INTERNAL_SERVER_ERROR, "the upload can't be written");
        }
        return Json(json!({"uploadId": upload_id, "filename": filename, "mimeType": mime_type, "size": size}))
            .into_response();
    }
}

/// Staged files older than `STAGED_FOR`: uploaded, never sent.
async fn sweep(staging: &Path) {
    let Ok(mut entries) = tokio::fs::read_dir(staging).await else { return };
    let now = SystemTime::now();
    while let Ok(Some(entry)) = entries.next_entry().await {
        let stale = entry
            .metadata()
            .await
            .and_then(|m| m.modified())
            .is_ok_and(|t| now.duration_since(t).is_ok_and(|age| age > STAGED_FOR));
        if stale {
            let _ = tokio::fs::remove_file(entry.path()).await;
        }
    }
}

/// A name safe to put in a directory: its last path component, no control
/// characters, never empty or a dot name.
fn clean_name(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or_default();
    let base: String = base.chars().filter(|c| !c.is_control()).collect();
    let base = base.trim();
    if base.is_empty() || base.chars().all(|c| c == '.') { "file".into() } else { base.into() }
}

/// A staged upload `startTask` names.
pub struct Staged {
    filename: String,
    mime_type: String,
    size: u64,
    bytes: PathBuf,
    meta: PathBuf,
}

impl Staged {
    pub fn filename(&self) -> &str {
        &self.filename
    }
}

/// The staged uploads by id, or why one can't be used.
pub fn staged(ids: &[String]) -> Result<Vec<Staged>, String> {
    let staging = crate::config::staging_dir();
    ids.iter()
        .map(|id| {
            let unknown = || format!("unknown or expired upload id: {id}");
            // Ids are uuids; anything that would step outside the staging
            // directory is no upload of ours.
            if id.is_empty() || id.contains(['/', '\\']) || id.starts_with('.') {
                return Err(unknown());
            }
            let bytes = staging.join(id);
            let meta = staging.join(format!("{id}.meta.json"));
            let read: Value = std::fs::read(&meta)
                .ok()
                .and_then(|m| serde_json::from_slice(&m).ok())
                .filter(|_| bytes.is_file())
                .ok_or_else(unknown)?;
            Ok(Staged {
                filename: clean_name(read["filename"].as_str().unwrap_or_default()),
                mime_type: read["mime_type"].as_str().unwrap_or("application/octet-stream").to_string(),
                size: read["size"].as_u64().unwrap_or_default(),
                bytes,
                meta,
            })
        })
        .collect()
}

/// An attachment as the job payload carries it to the turn.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Attachment {
    pub name: String,
    pub mime_type: String,
    pub size: u64,
    /// Absolute, under the conversation's uploads directory.
    pub path: String,
}

impl Attachment {
    /// What the message's chip shows it as.
    fn kind(&self) -> &'static str {
        ["image", "audio", "video"]
            .into_iter()
            .find(|kind| self.mime_type.starts_with(&format!("{kind}/")))
            .unwrap_or("document")
    }

    /// Sent to the model as an image, not only as a file.
    fn seen(&self) -> bool {
        IMAGE_TYPES.contains(&self.mime_type.as_str()) && self.size <= MAX_IMAGE_BYTES
    }

    /// Transcribed, when a transcription model is set.
    fn heard(&self) -> bool {
        self.mime_type.starts_with("audio/")
    }
}

/// Move each staged file into the conversation's uploads directory. A name
/// already there gets a ` (2)`, ` (3)` … before its extension.
pub fn take(staged: Vec<Staged>, conversation_id: &str) -> Result<Vec<Attachment>, String> {
    if staged.is_empty() {
        return Ok(vec![]);
    }
    let dir = crate::config::uploads_dir().join(conversation_id);
    std::fs::create_dir_all(&dir).map_err(|e| format!("the uploads directory can't be created: {e}"))?;
    staged
        .into_iter()
        .map(|s| {
            let dest = free_name(&dir, &s.filename);
            if std::fs::rename(&s.bytes, &dest).is_err() {
                // Staging and uploads may sit on different filesystems.
                std::fs::copy(&s.bytes, &dest).map_err(|e| format!("{} can't be saved: {e}", s.filename))?;
                let _ = std::fs::remove_file(&s.bytes);
            }
            let _ = std::fs::remove_file(&s.meta);
            let name = dest.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or(s.filename);
            Ok(Attachment { name, mime_type: s.mime_type, size: s.size, path: dest.to_string_lossy().into_owned() })
        })
        .collect()
}

fn free_name(dir: &Path, name: &str) -> PathBuf {
    let first = dir.join(name);
    if !first.exists() {
        return first;
    }
    let path = Path::new(name);
    let stem = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| name.into());
    let ext = path.extension().map(|e| format!(".{}", e.to_string_lossy())).unwrap_or_default();
    (2..).map(|n| dir.join(format!("{stem} ({n}){ext}"))).find(|p| !p.exists()).expect("some name is free")
}

/// The user message as stored: its text, then a chip per attachment — the
/// parts the message bubble renders.
pub fn display(query: &str, attachments: &[Attachment]) -> String {
    let mut parts = vec![json!({"type": "text", "text": query})];
    parts.extend(
        attachments.iter().map(|a| json!({"type": a.kind(), "name": a.name, "size": a.size, "mimeType": a.mime_type})),
    );
    crate::pyjson::dumps(&Value::Array(parts))
}

/// The turn's prompt: the query, a note of where each file is, the images
/// the model can see, and what was said in the audio when `transcription.model`
/// names a model (`transcribe.rs`). The blobs the image parts name are written,
/// and added to `blobs` for this turn's calls.
pub async fn prompt(
    pool: &SqlitePool,
    http: &reqwest::Client,
    query: &str,
    attachments: &[Attachment],
    blobs: &mut Blobs,
) -> Result<Content, String> {
    let transcriber = match attachments.iter().any(Attachment::heard) {
        true => crate::transcribe::model(pool).await,
        false => None,
    };
    let ends = crate::llm::Endpoints::from_env();
    let mut note = String::from("[Attached — saved in the workspace; open them with run_cell:");
    let mut transcripts = String::new();
    for a in attachments {
        let extra = match &transcriber {
            _ if a.seen() => ", also shown to you as an image".to_string(),
            Some(model) if a.heard() => {
                match crate::transcribe::transcribe(http, &ends, model, Path::new(&a.path)).await {
                    Ok(text) if text.trim().is_empty() => ", no speech in it".to_string(),
                    Ok(text) => {
                        let kept = crate::transcribe::transcript_path(Path::new(&a.path));
                        transcripts.push_str(&format!("\n\n[Transcript of {}]\n{}", a.name, clip(&text, &kept)));
                        format!(", transcript below and in {}", kept.display())
                    }
                    Err(e) => {
                        tracing::warn!("{} not transcribed: {e}", a.path);
                        format!(", couldn't be transcribed: {e}")
                    }
                }
            }
            _ => String::new(),
        };
        note.push_str(&format!("\n- {} ({}, {}{extra})", a.path, a.mime_type, human_size(a.size)));
    }
    note.push(']');
    note.push_str(&transcripts);
    let text = if query.trim().is_empty() { note } else { format!("{query}\n\n{note}") };
    let mut parts = vec![Part::Typed(Typed::Text { text, signature: None, extras: Default::default() })];
    for a in attachments.iter().filter(|a| a.seen()) {
        let bytes = match tokio::fs::read(&a.path).await {
            Ok(bytes) => bytes,
            // Gone since it was sent: the note still names it, and run_cell
            // will say it's missing.
            Err(e) => {
                tracing::warn!("attachment {} unreadable: {e}", a.path);
                continue;
            }
        };
        let hash = format!("sha256:{:x}", Sha256::digest(&bytes));
        sqlx::query(
            "INSERT OR IGNORE INTO transcript_blobs (hash, mime_type, size, data, created_at) VALUES (?, ?, ?, ?, ?)",
        )
        .bind(&hash)
        .bind(&a.mime_type)
        .bind(bytes.len() as i64)
        .bind(&bytes)
        .bind(now_stored())
        .execute(pool)
        .await
        .map_err(|e| e.to_string())?;
        blobs.insert(hash.clone(), STANDARD.encode(&bytes));
        parts.push(Part::Typed(Typed::Image(Media {
            mime_type: Some(a.mime_type.clone()),
            blob: Some(hash),
            data: None,
            url: None,
            extras: Default::default(),
        })));
    }
    Ok(Content::Parts(parts))
}

/// A transcript as the prompt carries it: whole up to `MAX_TRANSCRIPT_CHARS`,
/// past that cut, with where the rest is.
fn clip(text: &str, kept: &Path) -> String {
    match text.char_indices().nth(MAX_TRANSCRIPT_CHARS) {
        None => text.to_string(),
        Some((at, _)) => format!("{}…\n[cut at {MAX_TRANSCRIPT_CHARS} characters — the whole transcript is in {}]", &text[..at], kept.display()),
    }
}

fn human_size(bytes: u64) -> String {
    match bytes {
        b if b < 1024 => format!("{b} B"),
        b if b < 1024 * 1024 => format!("{:.0} KB", b as f64 / 1024.0),
        b => format!("{:.1} MB", b as f64 / (1024.0 * 1024.0)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_name_keeps_only_a_safe_last_component() {
        assert_eq!(clean_name("report.pdf"), "report.pdf");
        assert_eq!(clean_name("../../etc/passwd"), "passwd");
        assert_eq!(clean_name("C:\\Users\\me\\a b.png"), "a b.png");
        assert_eq!(clean_name(".."), "file");
        assert_eq!(clean_name(""), "file");
        assert_eq!(clean_name("x\u{0}y\n.txt"), "xy.txt");
    }

    #[test]
    fn free_name_numbers_a_taken_name() {
        let dir = std::env::temp_dir().join(format!("jarvis-uploads-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(free_name(&dir, "a.png"), dir.join("a.png"));
        std::fs::write(dir.join("a.png"), b"").unwrap();
        assert_eq!(free_name(&dir, "a.png"), dir.join("a (2).png"));
        std::fs::write(dir.join("a (2).png"), b"").unwrap();
        assert_eq!(free_name(&dir, "a.png"), dir.join("a (3).png"));
        assert_eq!(free_name(&dir, "noext"), dir.join("noext"));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_small_common_images_are_seen() {
        let a = |mime: &str, size| Attachment { name: "x".into(), mime_type: mime.into(), size, path: "/x".into() };
        assert!(a("image/png", 1000).seen());
        assert!(!a("image/png", MAX_IMAGE_BYTES + 1).seen());
        assert!(!a("image/svg+xml", 10).seen());
        assert!(!a("application/pdf", 10).seen());
        assert_eq!(a("application/pdf", 10).kind(), "document");
        assert_eq!(a("image/svg+xml", 10).kind(), "image");
    }
}
