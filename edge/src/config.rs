//! Process configuration, resolved the way `core/config.py` resolves it so the
//! edge and the Python backend always agree on which database they share.

use std::net::SocketAddr;
use std::path::PathBuf;

pub struct Config {
    /// Where the edge listens. It takes over the port the Python server used
    /// to own, so the frontend, the vite proxy and the `jarvis` SDK
    /// (`JARVIS_API_URL`) keep working without changes.
    pub bind: SocketAddr,
    /// The Python server, which now listens behind the edge.
    pub backend: String,
    pub db_path: PathBuf,
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        // Same `.env` the Python side loads, so WORK_DIR / DATABASE_URL set
        // there apply to both processes.
        let _ = dotenvy::dotenv();

        let bind = env_or("JARVIS_EDGE_BIND", "127.0.0.1:8000")
            .parse()
            .map_err(|e| format!("JARVIS_EDGE_BIND: {e}"))?;
        let backend = env_or("JARVIS_BACKEND_URL", "http://127.0.0.1:8001")
            .trim_end_matches('/')
            .to_string();
        if !backend.starts_with("http://") {
            return Err(format!("JARVIS_BACKEND_URL must be an http:// URL, got {backend}"));
        }
        Ok(Self { bind, backend, db_path: db_path()? })
    }

    /// `ws://` twin of `backend`, for proxying WebSocket upgrades.
    pub fn backend_ws(&self) -> String {
        format!("ws://{}", &self.backend["http://".len()..])
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.to_string())
}

/// `DATABASE_URL` (a SQLAlchemy URL) wins, else `$WORK_DIR/database.db`,
/// else `~/.jarvis/database.db` — mirroring `AppConfig.from_env`.
fn db_path() -> Result<PathBuf, String> {
    if let Ok(url) = std::env::var("DATABASE_URL") {
        if !url.is_empty() {
            let (scheme, path) = url
                .split_once(":///")
                .ok_or_else(|| format!("DATABASE_URL is not a file URL: {url}"))?;
            if !scheme.starts_with("sqlite") {
                return Err(format!("the edge only supports sqlite, got {scheme}"));
            }
            return Ok(PathBuf::from(path));
        }
    }
    let work_dir = match std::env::var("WORK_DIR") {
        Ok(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => {
            let home = std::env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
            PathBuf::from(home).join(".jarvis")
        }
    };
    Ok(work_dir.join("database.db"))
}
