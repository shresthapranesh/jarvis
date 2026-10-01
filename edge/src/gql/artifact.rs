//! Artifact, ArtifactVersion and Document — `server/graphql/types/artifact.py`,
//! `types/document.py` and `queries/artifact.py`.

use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::EdgeData;
use super::codec::{DateTime, decode_global_id, global_id, new_id, now_stored};
use super::write::update_row;

/// A file-backed body, read the way `Path.read_text(encoding="utf-8")` does:
/// a missing file or non-UTF-8 bytes read as "", and newlines are
/// universal (`\r\n` and lone `\r` become `\n`). Any other IO error surfaces
/// as a field error, as it does in Python.
fn read_text(path: &str) -> Result<String> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(match String::from_utf8(bytes) {
            Ok(text) if text.contains('\r') => text.replace("\r\n", "\n").replace('\r', "\n"),
            Ok(text) => text,
            Err(_) => String::new(),
        }),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(String::new()),
        Err(e) => Err(e.into()),
    }
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Artifact {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub title: String,
    pub filename: String,
    pub kind: String,
    pub mime_type: Option<String>,
    pub conversation_id: Option<String>,
    pub message_id: Option<String>,
    pub created_at: DateTime,
    pub updated_at: DateTime,
}

const ARTIFACT_COLUMNS: &str =
    "id, title, filename, kind, mime_type, conversation_id, message_id, created_at, updated_at";

impl Artifact {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl Artifact {
    pub async fn id(&self) -> ID {
        global_id("Artifact", &self.raw_id)
    }

    /// File-backed body, read on demand so list queries stay cheap. Binary
    /// kinds read as "" — their bytes come from the raw download endpoint.
    async fn content(&self) -> Result<String> {
        read_text(&self.filename)
    }

    async fn versions(&self, ctx: &Context<'_>) -> Result<Vec<ArtifactVersion>> {
        ArtifactVersion::for_artifact(ctx.data()?, &self.raw_id).await
    }

    /// Counts `{id}_v*` files in the artifacts directory, as the Python
    /// resolver globs — not the version rows.
    async fn version_count(&self, ctx: &Context<'_>) -> Result<i64> {
        let dir = &ctx.data::<EdgeData>()?.artifacts_dir;
        let prefix = format!("{}_v", self.raw_id);
        Ok(count_prefixed(dir, &prefix))
    }
}

fn count_prefixed(dir: &Path, prefix: &str) -> i64 {
    std::fs::read_dir(dir).map_or(0, |entries| {
        entries
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().starts_with(prefix))
            .count() as i64
    })
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct ArtifactVersion {
    pub id: String,
    pub artifact_id: String,
    pub version: i64,
    pub title: String,
    pub filename: String,
    pub created_at: DateTime,
}

impl ArtifactVersion {
    async fn for_artifact(pool: &SqlitePool, artifact_id: &str) -> Result<Vec<Self>> {
        Ok(sqlx::query_as(
            "SELECT id, artifact_id, version, title, filename, created_at FROM artifact_versions \
             WHERE artifact_id = ? ORDER BY version ASC",
        )
        .bind(artifact_id)
        .fetch_all(pool)
        .await?)
    }
}

#[ComplexObject]
impl ArtifactVersion {
    async fn content(&self) -> Result<String> {
        read_text(&self.filename)
    }
}

#[derive(SimpleObject, sqlx::FromRow, Clone)]
#[graphql(complex)]
pub struct Document {
    #[graphql(skip)]
    #[sqlx(rename = "id")]
    pub raw_id: String,
    pub conversation_id: String,
    pub message_id: Option<String>,
    pub filename: String,
    pub mime_type: String,
    pub size: i64,
    pub created_at: DateTime,
}

const DOCUMENT_COLUMNS: &str = "id, conversation_id, message_id, filename, mime_type, size, created_at";

impl Document {
    pub async fn by_id(pool: &SqlitePool, raw_id: &str) -> Result<Option<Self>> {
        Ok(sqlx::query_as(&format!("SELECT {DOCUMENT_COLUMNS} FROM documents WHERE id = ?"))
            .bind(raw_id)
            .fetch_optional(pool)
            .await?)
    }
}

#[ComplexObject]
impl Document {
    pub async fn id(&self) -> ID {
        global_id("Document", &self.raw_id)
    }
}

#[derive(Default)]
pub struct ArtifactQuery;

#[Object]
impl ArtifactQuery {
    async fn artifacts(&self, ctx: &Context<'_>, conversation_id: Option<String>) -> Result<Vec<Artifact>> {
        let pool: &SqlitePool = ctx.data()?;
        let mut sql = format!("SELECT {ARTIFACT_COLUMNS} FROM artifacts");
        if conversation_id.is_some() {
            sql.push_str(" WHERE conversation_id = ?");
        }
        sql.push_str(" ORDER BY updated_at DESC");
        let mut q = sqlx::query_as(&sql);
        if let Some(c) = &conversation_id {
            q = q.bind(c);
        }
        Ok(q.fetch_all(pool).await?)
    }

    async fn artifact(&self, ctx: &Context<'_>, id: ID) -> Result<Option<Artifact>> {
        let (_, raw) = decode_global_id(&id)?;
        Artifact::by_id(ctx.data()?, &raw).await
    }

    async fn documents(&self, ctx: &Context<'_>, conversation_id: String) -> Result<Vec<Document>> {
        Ok(sqlx::query_as(&format!(
            "SELECT {DOCUMENT_COLUMNS} FROM documents WHERE conversation_id = ? ORDER BY created_at ASC"
        ))
        .bind(conversation_id)
        .fetch_all(ctx.data::<SqlitePool>()?)
        .await?)
    }

    async fn artifact_versions(&self, ctx: &Context<'_>, artifact_id: String) -> Result<Vec<ArtifactVersion>> {
        ArtifactVersion::for_artifact(ctx.data()?, &artifact_id).await
    }
}

/// Python's `Path(p).suffix`: the last `.ext` of the file name, "" for none
/// (a dotfile's leading dot, or a trailing bare dot, isn't one).
fn suffix(path: &str) -> String {
    match Path::new(path).extension().and_then(|e| e.to_str()) {
        Some(ext) if !ext.is_empty() => format!(".{ext}"),
        _ => String::new(),
    }
}

/// `core/artifact_storage.py:artifact_path` / `version_path`.
fn live_path(dir: &Path, id: &str, ext: &str) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    Ok(dir.join(format!("{id}{ext}")))
}

fn version_path(dir: &Path, id: &str, version: i64, ext: &str) -> std::io::Result<PathBuf> {
    std::fs::create_dir_all(dir)?;
    Ok(dir.join(format!("{id}_v{version}{ext}")))
}

async fn latest_version(pool: &SqlitePool, artifact_id: &str) -> Result<i64> {
    let (v,): (Option<i64>,) = sqlx::query_as("SELECT MAX(version) FROM artifact_versions WHERE artifact_id = ?")
        .bind(artifact_id)
        .fetch_one(pool)
        .await?;
    Ok(v.unwrap_or(0))
}

async fn insert_version(pool: &SqlitePool, artifact_id: &str, title: &str, filename: &Path, version: i64) -> Result<()> {
    sqlx::query(
        "INSERT INTO artifact_versions (id, artifact_id, version, title, filename, created_at) VALUES (?, ?, ?, ?, ?, ?)",
    )
    .bind(new_id())
    .bind(artifact_id)
    .bind(version)
    .bind(title)
    .bind(filename.to_string_lossy())
    .bind(now_stored())
    .execute(pool)
    .await?;
    Ok(())
}

/// Unlink, ignoring every failure — a missing file is already the goal.
fn remove_quietly(path: &str) {
    let _ = std::fs::remove_file(path);
}

#[derive(Default)]
pub struct ArtifactMutation;

#[Object]
impl ArtifactMutation {
    // Retitle and/or replace a markdown body. A new body is versioned the
    // way `write_artifact` versions it: an artifact with no history gets its
    // current file saved as v1 first, then the new body becomes v(n+1).
    async fn update_artifact(
        &self,
        ctx: &Context<'_>,
        id: ID,
        title: Option<String>,
        content: Option<String>,
    ) -> Result<Artifact> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let art = Artifact::by_id(pool, &raw).await?.ok_or("artifact not found")?;
        // Python mutates the identity-mapped row in place, so every later
        // read of the title sees the new one.
        let current_title = title.clone().unwrap_or(art.title.clone());
        if let Some(t) = &title {
            update_row(pool, "artifacts", &raw, &[("title", t.clone())]).await?;
        }
        if let Some(content) = &content {
            if art.kind != "markdown" {
                return Err(
                    "binary artifacts can't be edited inline — recreate via write_artifact(file_path=...)".into()
                );
            }
            let dir = &ctx.data::<EdgeData>()?.artifacts_dir;
            let live = live_path(dir, &raw, ".md")?;
            let mut latest = latest_version(pool, &raw).await?;
            if latest == 0 && live.exists() {
                // Migrate the history-less file as v1. Its copy is best-effort,
                // the row is not — exactly as in Python.
                let v1 = version_path(dir, &raw, 1, ".md")?;
                if !v1.exists() {
                    // `read_text` + `write_text`: newlines normalized, and a
                    // body that isn't UTF-8 raises — swallowed, so no file.
                    if let Ok(Ok(old)) = std::fs::read(&live).map(String::from_utf8) {
                        let _ = std::fs::write(&v1, old.replace("\r\n", "\n").replace('\r', "\n"));
                    }
                }
                insert_version(pool, &raw, &current_title, &v1, 1).await?;
                latest = 1;
            }
            std::fs::write(&live, content)?;
            let new_ver = latest + 1;
            let ver = version_path(dir, &raw, new_ver, ".md")?;
            let _ = std::fs::write(&ver, content);
            insert_version(pool, &raw, &current_title, &ver, new_ver).await?;
            update_row(pool, "artifacts", &raw, &[]).await?;
        }
        Artifact::by_id(pool, &raw).await?.ok_or_else(|| "artifact not found".into())
    }

    // Copy a version's bytes back as the live file, recorded as a new version.
    async fn restore_artifact_version(&self, ctx: &Context<'_>, id: ID, version: i32) -> Result<Artifact> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let art = Artifact::by_id(pool, &raw).await?.ok_or("artifact not found")?;
        let ver: Option<(String, String)> =
            sqlx::query_as("SELECT title, filename FROM artifact_versions WHERE artifact_id = ? AND version = ?")
                .bind(&raw)
                .bind(version)
                .fetch_optional(pool)
                .await?;
        let (ver_title, ver_file) = ver.ok_or_else(|| format!("version {version} not found for artifact {raw}"))?;
        let data = std::fs::read(&ver_file).map_err(|e| format!("failed to read version file: {e}"))?;

        let ext = [suffix(&ver_file), suffix(&art.filename)]
            .into_iter()
            .find(|e| !e.is_empty())
            .unwrap_or_else(|| ".bin".to_string());
        let dir = &ctx.data::<EdgeData>()?.artifacts_dir;
        let live = live_path(dir, &raw, &ext)?;
        let latest = latest_version(pool, &raw).await?;
        std::fs::write(&live, &data)?;
        let new_ver = latest + 1;
        let ver_path = version_path(dir, &raw, new_ver, &ext)?;
        let _ = std::fs::write(&ver_path, &data);
        insert_version(pool, &raw, &ver_title, &ver_path, new_ver).await?;
        update_row(
            pool,
            "artifacts",
            &raw,
            &[("title", ver_title), ("filename", live.to_string_lossy().into_owned())],
        )
        .await?;
        Artifact::by_id(pool, &raw).await?.ok_or_else(|| "artifact not found".into())
    }

    // The row and its versions, then their files.
    async fn delete_artifact(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let art = Artifact::by_id(pool, &raw).await?.ok_or("artifact not found")?;
        let versions: Vec<(String,)> = sqlx::query_as("SELECT filename FROM artifact_versions WHERE artifact_id = ?")
            .bind(&raw)
            .fetch_all(pool)
            .await?;
        let mut tx = pool.begin().await?;
        sqlx::query("DELETE FROM artifact_versions WHERE artifact_id = ?").bind(&raw).execute(&mut *tx).await?;
        sqlx::query("DELETE FROM artifacts WHERE id = ?").bind(&raw).execute(&mut *tx).await?;
        tx.commit().await?;
        remove_quietly(&art.filename);
        for (path,) in versions {
            remove_quietly(&path);
        }
        Ok(true)
    }

    // The row and its indexed chunks, then the uploaded file.
    async fn delete_document(&self, ctx: &Context<'_>, id: ID) -> Result<bool> {
        let (_, raw) = decode_global_id(&id)?;
        let pool: &SqlitePool = ctx.data()?;
        let path: Option<(String,)> =
            sqlx::query_as("SELECT path FROM documents WHERE id = ?").bind(&raw).fetch_optional(pool).await?;
        let (path,) = path.ok_or("document not found")?;
        let mut tx = pool.begin().await?;
        sqlx::query("DELETE FROM document_chunks WHERE document_id = ?").bind(&raw).execute(&mut *tx).await?;
        sqlx::query("DELETE FROM documents WHERE id = ?").bind(&raw).execute(&mut *tx).await?;
        tx.commit().await?;
        remove_quietly(&path);
        Ok(true)
    }
}
