//! Embeddings — `core/doc_index.py:get_embedder` and `aembed_query_cached`.
//!
//! The embedder Python would pick: Gemini (`batchEmbedContents`, the request
//! the google-genai SDK sends) when `GOOGLE_API_KEY` is set, else Ollama's
//! `/api/embed`. The model is the `embedding.model` setting, else
//! `gemini-embedding-001` (Ollama: `nomic-embed-text`). Vectors come back as
//! float32, the way they're stored (`Memory.embedding`).
//!
//! A query is embedded in retrieval space and a stored text in document space,
//! as LangChain's `embed_query` / `embed_documents` do.

use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use sqlx::SqlitePool;

const DEFAULT_MODEL: &str = "models/gemini-embedding-001";
const OLLAMA_DEFAULT: &str = "nomic-embed-text";
/// `_QUERY_CACHE_MAX` / `_QUERY_CACHE_TTL`.
const CACHE_MAX: usize = 512;
const CACHE_TTL: Duration = Duration::from_secs(3600);

#[derive(Clone, Copy, PartialEq)]
pub enum Space {
    Query,
    Document,
}

pub struct Embedder {
    kind: Kind,
}

enum Kind {
    Google { base: String, key: String, model: String },
    Ollama { base: String, model: String },
}

impl Embedder {
    /// `get_embedder`, with the `embedding.model` setting applied.
    pub async fn resolve(pool: &SqlitePool) -> Self {
        let configured = crate::catalog::setting(pool, "embedding.model").await.ok().flatten().filter(|m| !m.is_empty());
        let var = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        if let Some(key) = var("GOOGLE_API_KEY") {
            let model = configured.clone().unwrap_or_else(|| DEFAULT_MODEL.into());
            let model = if model.starts_with("models/") { model } else { format!("models/{model}") };
            let base = var("JARVIS_GOOGLE_BASE_URL")
                .unwrap_or_else(|| "https://generativelanguage.googleapis.com".into())
                .trim_end_matches('/')
                .to_string();
            return Embedder { kind: Kind::Google { base, key, model } };
        }
        let model = configured.filter(|m| !m.starts_with("models/")).unwrap_or_else(|| OLLAMA_DEFAULT.into());
        Embedder { kind: Kind::Ollama { base: crate::llm::ollama::host(var("OLLAMA_HOST").as_deref()), model } }
    }

    /// The model, as the query cache keys it (`_effective_model`).
    fn cache_model(&self) -> &str {
        match &self.kind {
            Kind::Google { model, .. } => model,
            Kind::Ollama { model, .. } => model,
        }
    }

    pub async fn embed(&self, http: &reqwest::Client, text: &str, space: Space) -> Result<Vec<f32>, String> {
        let (url, body, key) = match &self.kind {
            Kind::Google { base, key, model } => {
                let name = model.trim_start_matches("models/");
                let mut content = json!({"parts": [{"text": text}]});
                if space == Space::Query {
                    content["role"] = "user".into();
                }
                let task = if space == Space::Query { "RETRIEVAL_QUERY" } else { "RETRIEVAL_DOCUMENT" };
                let body = json!({"requests": [{"content": content, "taskType": task, "model": model}]});
                (format!("{base}/v1beta/models/{name}:batchEmbedContents"), body, Some(key))
            }
            Kind::Ollama { base, model } => (format!("{base}/api/embed"), json!({"model": model, "input": [text]}), None),
        };
        let mut req = http.post(url).json(&body).timeout(Duration::from_secs(60));
        if let Some(key) = key {
            req = req.header("x-goog-api-key", key);
        }
        let resp = req.send().await.map_err(|e| e.to_string())?;
        let status = resp.status();
        let body: Value = resp.json().await.map_err(|e| e.to_string())?;
        if !status.is_success() {
            return Err(format!("{}: {body}", status.as_u16()));
        }
        let values = match &self.kind {
            Kind::Google { .. } => &body["embeddings"][0]["values"],
            Kind::Ollama { .. } => &body["embeddings"][0],
        };
        values
            .as_array()
            .and_then(|v| v.iter().map(|x| x.as_f64().map(|f| f as f32)).collect::<Option<Vec<_>>>())
            .ok_or_else(|| format!("no embedding in {body}"))
    }

    /// `aembed_query_cached(query, allow_trivial=…)`: a query's vector,
    /// cached an hour by model and whitespace-collapsed text. `None` on a
    /// trivial or too-short query (unless allowed), or a failed call — the
    /// caller then runs without the dense arm.
    pub async fn query(&self, http: &reqwest::Client, query: &str, allow_trivial: bool) -> Option<Vec<f32>> {
        if !allow_trivial && super::prompt::trivial(query) {
            return None;
        }
        let normalized = query.split_whitespace().collect::<Vec<_>>().join(" ");
        if normalized.chars().count() < 3 && !allow_trivial {
            return None;
        }
        let key = format!("{}::{normalized}", self.cache_model());
        if let Some(v) = CACHE.lock().expect("cache lock").get(&key) {
            return Some(v);
        }
        match self.embed(http, query, Space::Query).await {
            Ok(v) => {
                CACHE.lock().expect("cache lock").put(key, v.clone());
                Some(v)
            }
            Err(e) => {
                tracing::warn!("query embedding failed: {e}");
                None
            }
        }
    }
}

struct Cache {
    entries: HashMap<String, (Vec<f32>, Instant)>,
    order: VecDeque<String>,
}

impl Cache {
    fn get(&mut self, key: &str) -> Option<Vec<f32>> {
        let (v, at) = self.entries.get(key)?;
        if at.elapsed() >= CACHE_TTL {
            self.entries.remove(key);
            self.order.retain(|k| k != key);
            return None;
        }
        let v = v.clone();
        self.order.retain(|k| k != key);
        self.order.push_back(key.to_string());
        Some(v)
    }

    fn put(&mut self, key: String, v: Vec<f32>) {
        self.order.retain(|k| *k != key);
        self.order.push_back(key.clone());
        self.entries.insert(key, (v, Instant::now()));
        while self.order.len() > CACHE_MAX {
            if let Some(old) = self.order.pop_front() {
                self.entries.remove(&old);
            }
        }
    }
}

static CACHE: std::sync::LazyLock<Mutex<Cache>> =
    std::sync::LazyLock::new(|| Mutex::new(Cache { entries: HashMap::new(), order: VecDeque::new() }));

/// A stored float32 vector (`np.frombuffer(blob, dtype=np.float32)`).
pub fn from_blob(blob: &[u8]) -> Vec<f32> {
    blob.chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect()
}

/// `np.asarray(vec, dtype=np.float32).tobytes()`.
pub fn to_blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}
