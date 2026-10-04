//! `write_artifact` — a port of `tools/artifacts.py`; a change to either is
//! made in both. A deliverable the user keeps: a markdown body or a file the
//! agent already wrote, stored under the artifact directory as the live file
//! plus one copy per version, with an `artifacts` row and one
//! `artifact_versions` row per version.

use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use sqlx::SqlitePool;

use crate::gql::codec::{new_id, now_stored};

/// `MAX_ARTIFACT_FILE_BYTES`.
const MAX_FILE_BYTES: u64 = 50 * 1024 * 1024;

/// Where the call came from: `ToolContext`'s ids.
pub struct Scope<'a> {
    pub pool: &'a SqlitePool,
    pub dir: &'a Path,
    /// Relative `file_path`s resolve here — the Python server's working
    /// directory, and the kernel's.
    pub cwd: &'a Path,
    pub conversation_id: Option<&'a str>,
    /// The assistant message the run writes (chat only).
    pub message_id: Option<&'a str>,
}

/// The tool's answer, and the `artifact` event it announces (none for a
/// refusal).
pub struct Written {
    pub answer: String,
    pub event: Option<Value>,
}

fn refused(error: String) -> Written {
    Written { answer: crate::pyjson::dumps(&json!({"error": error})), event: None }
}

/// `write_artifact(title, content, file_path, artifact_id)`. An `Err` is a
/// failure Python would raise from (the database, the live file).
pub async fn write(
    scope: &Scope<'_>,
    title: &str,
    content: Option<&str>,
    file_path: Option<&str>,
    artifact_id: Option<&str>,
) -> Result<Written, String> {
    match (content, file_path) {
        (Some(content), None) => markdown(scope, title, content, artifact_id).await,
        (None, Some(path)) => file(scope, title, path, artifact_id).await,
        _ => Ok(refused("pass exactly one of content= (markdown) or file_path= (a file on disk)".into())),
    }
}

fn live_path(dir: &Path, id: &str, ext: &str) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    Ok(dir.join(format!("{id}{ext}")))
}

fn version_path(dir: &Path, id: &str, version: i64, ext: &str) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    Ok(dir.join(format!("{id}_v{version}{ext}")))
}

async fn existing_title(pool: &SqlitePool, id: &str) -> Result<Option<String>, String> {
    sqlx::query_scalar("SELECT title FROM artifacts WHERE id = ?").bind(id).fetch_optional(pool).await.map_err(|e| e.to_string())
}

async fn latest_version(pool: &SqlitePool, id: &str) -> Result<i64, String> {
    let v: Option<i64> = sqlx::query_scalar("SELECT MAX(version) FROM artifact_versions WHERE artifact_id = ?")
        .bind(id)
        .fetch_one(pool)
        .await
        .map_err(|e| e.to_string())?;
    Ok(v.unwrap_or(0))
}

async fn insert_version(pool: &SqlitePool, id: &str, title: &str, path: &Path, version: i64) -> sqlx::Result<()> {
    sqlx::query("INSERT INTO artifact_versions (id, artifact_id, version, title, filename, created_at) VALUES (?, ?, ?, ?, ?, ?)")
        .bind(new_id())
        .bind(id)
        .bind(version)
        .bind(title)
        .bind(path.to_string_lossy())
        .bind(now_stored())
        .execute(pool)
        .await?;
    Ok(())
}

/// A version's copy and its row, best-effort: a failure is logged and the
/// artifact stands without that version, as in Python.
async fn snapshot(pool: &SqlitePool, dir: &Path, id: &str, title: &str, version: i64, ext: &str, data: &[u8]) {
    let saved = async {
        let path = version_path(dir, id, version, ext)?;
        std::fs::write(&path, data).map_err(|e| e.to_string())?;
        insert_version(pool, id, title, &path, version).await.map_err(|e| e.to_string())
    };
    if let Err(e) = saved.await {
        tracing::warn!("artifact version save failed for {id} v{version}: {e}");
    }
}

async fn insert_artifact(
    scope: &Scope<'_>,
    id: &str,
    title: &str,
    live: &Path,
    kind: &str,
    mime_type: Option<&str>,
) -> Result<(), String> {
    let now = now_stored();
    sqlx::query(
        "INSERT INTO artifacts (id, title, filename, kind, mime_type, conversation_id, message_id, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(id)
    .bind(title)
    .bind(live.to_string_lossy())
    .bind(kind)
    .bind(mime_type)
    .bind(scope.conversation_id)
    .bind(scope.message_id)
    .bind(&now)
    .bind(&now)
    .execute(scope.pool)
    .await
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// `_write_markdown_artifact`.
async fn markdown(scope: &Scope<'_>, title: &str, content: &str, artifact_id: Option<&str>) -> Result<Written, String> {
    let (pool, dir) = (scope.pool, scope.dir);
    let (id, action) = match artifact_id.filter(|a| !a.is_empty()) {
        Some(id) => {
            let Some(old_title) = existing_title(pool, id).await? else {
                return Ok(refused(format!("artifact {id} not found")));
            };
            let live = live_path(dir, id, ".md")?;
            let mut latest = latest_version(pool, id).await?;
            if latest == 0 && live.exists() {
                // An artifact from before versioning: its file becomes v1
                // under its old title. `read_text` raises on bytes that
                // aren't UTF-8, which skips the migration altogether.
                let migrated = async {
                    let old = String::from_utf8(std::fs::read(&live).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
                    let v1 = version_path(dir, id, 1, ".md")?;
                    std::fs::write(&v1, old.replace("\r\n", "\n").replace('\r', "\n")).map_err(|e| e.to_string())?;
                    insert_version(pool, id, &old_title, &v1, 1).await.map_err(|e| e.to_string())
                };
                match migrated.await {
                    Ok(()) => latest = 1,
                    Err(e) => tracing::warn!("artifact version migration failed for {id}: {e}"),
                }
            }
            crate::gql::write::update_row(pool, "artifacts", id, &[("title", title.to_string())])
                .await
                .map_err(|e| e.to_string())?;
            std::fs::write(&live, content).map_err(|e| e.to_string())?;
            snapshot(pool, dir, id, title, latest + 1, ".md", content.as_bytes()).await;
            (id.to_string(), "updated")
        }
        None => {
            let id = new_id();
            let live = live_path(dir, &id, ".md")?;
            insert_artifact(scope, &id, title, &live, "markdown", None).await?;
            std::fs::write(&live, content).map_err(|e| e.to_string())?;
            snapshot(pool, dir, &id, title, 1, ".md", content.as_bytes()).await;
            (id, "created")
        }
    };
    let preview: String = content.chars().take(300).collect();
    Ok(Written {
        answer: crate::pyjson::dumps(&json!({"id": id, "title": title, "action": action})),
        event: Some(json!({
            "action": action, "id": id, "title": title, "kind": "markdown",
            "preview": preview, "conversation_id": scope.conversation_id,
        })),
    })
}

/// `PurePath.suffix`.
fn suffix(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[i..],
        _ => "",
    }
}

/// `_write_file_artifact`: a file already on disk, copied in.
async fn file(scope: &Scope<'_>, title: &str, file_path: &str, artifact_id: Option<&str>) -> Result<Written, String> {
    let (pool, dir) = (scope.pool, scope.dir);
    let src = scope.cwd.join(file_path);
    let meta = match std::fs::metadata(&src) {
        Ok(m) if m.is_file() => m,
        _ => return Ok(refused(format!("file not found: {file_path}"))),
    };
    let size = meta.len();
    if size > MAX_FILE_BYTES {
        return Ok(refused(format!(
            "file is {size} bytes, exceeds the {} MiB artifact cap",
            MAX_FILE_BYTES / (1024 * 1024)
        )));
    }
    let name = src.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let ext = match suffix(&name) {
        "" => ".bin".to_string(),
        s => s.to_string(),
    };
    let mime_type = crate::mimetypes::guess_type(&name);
    let kind = crate::mimetypes::infer_kind(mime_type.as_deref(), &ext);
    let data = std::fs::read(&src).map_err(|e| e.to_string())?;

    let (id, action) = match artifact_id.filter(|a| !a.is_empty()) {
        Some(id) => {
            if existing_title(pool, id).await?.is_none() {
                return Ok(refused(format!("artifact {id} not found")));
            }
            let live = live_path(dir, id, &ext)?;
            let latest = latest_version(pool, id).await?;
            // The row keeps its filename, as Python's update leaves it.
            sqlx::query("UPDATE artifacts SET title = ?, kind = ?, mime_type = ?, updated_at = ? WHERE id = ?")
                .bind(title)
                .bind(kind)
                .bind(mime_type.as_deref())
                .bind(now_stored())
                .bind(id)
                .execute(pool)
                .await
                .map_err(|e| e.to_string())?;
            std::fs::write(&live, &data).map_err(|e| e.to_string())?;
            snapshot(pool, dir, id, title, latest + 1, &ext, &data).await;
            (id.to_string(), "updated")
        }
        None => {
            let id = new_id();
            let live = live_path(dir, &id, &ext)?;
            insert_artifact(scope, &id, title, &live, kind, mime_type.as_deref()).await?;
            std::fs::write(&live, &data).map_err(|e| e.to_string())?;
            snapshot(pool, dir, &id, title, 1, &ext, &data).await;
            (id, "created")
        }
    };
    let preview = format!("[{kind} · {} · {size} bytes]", mime_type.as_deref().unwrap_or("unknown"));
    Ok(Written {
        answer: crate::pyjson::dumps(&json!({
            "id": id, "title": title, "action": action, "kind": kind, "mime_type": mime_type, "size": size,
        })),
        event: Some(json!({
            "action": action, "id": id, "title": title, "kind": kind,
            "preview": preview, "conversation_id": scope.conversation_id,
        })),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suffix_is_pathlibs() {
        for (name, want) in [("a.", ""), ("..x", ".x"), (".bashrc", ""), ("a.tar.gz", ".gz"), ("A.PNG", ".PNG"), ("noext", "")] {
            assert_eq!(suffix(name), want, "{name}");
        }
    }
}
