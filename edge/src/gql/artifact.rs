//! Artifact, ArtifactVersion and Document — `server/graphql/types/artifact.py`,
//! `types/document.py` and `queries/artifact.py`.

use std::io::ErrorKind;
use std::path::Path;

use async_graphql::{ComplexObject, Context, ID, Object, Result, SimpleObject};
use sqlx::SqlitePool;

use super::EdgeData;
use super::codec::{DateTime, decode_global_id, global_id};

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
