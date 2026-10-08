//! Process configuration, resolved the way `core/config.py` resolves it, so
//! the kernels' `jarvis` SDK and the server agree on which database they share.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};

pub struct Config {
    /// Where the server listens: the port the frontend, the vite proxy and
    /// the `jarvis` SDK (`JARVIS_API_URL`) use.
    pub bind: SocketAddr,
    pub db_path: PathBuf,
    /// Where artifact files live (`AppConfig.artifacts_dir`).
    pub artifacts_dir: PathBuf,
    /// LangGraph's database (`AppConfig.checkpoints_db`): its store is
    /// copied into `kv_store` once (`schema::import_store_once`).
    pub checkpoints_db: PathBuf,
    /// The built SPA (`static/dist` under the app), when it exists.
    pub static_dir: Option<PathBuf>,
    /// The jarvis checkout: kernels run there, with it on `sys.path`.
    pub app_dir: PathBuf,
    /// The interpreter kernels run on: `JARVIS_KERNEL_PYTHON`, else the
    /// checkout's `.venv`, else `python3` on the PATH.
    pub kernel_python: PathBuf,
    /// `WORK_DIR`, as given: the browser's default profile is under it.
    pub work_dir: PathBuf,
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        // Same `.env` the kernels' Python loads, so WORK_DIR / DATABASE_URL
        // set there apply to both.
        let _ = dotenvy::dotenv();

        let bind: SocketAddr = env_or("JARVIS_EDGE_BIND", "127.0.0.1:8000")
            .parse()
            .map_err(|e| format!("JARVIS_EDGE_BIND: {e}"))?;
        // Resolved, as Python's is: an artifact's stored path is under it.
        let artifacts_dir = resolve(match std::env::var("ARTIFACTS_DIR") {
            Ok(dir) if !dir.is_empty() => PathBuf::from(dir),
            _ => resolve(work_dir()?).join("artifacts"),
        });
        let checkpoints_db = env_path("CHECKPOINTS_DB").unwrap_or(work_dir()?.join("checkpoints.db"));
        // The jarvis checkout: where kernels run, and where the SPA was built.
        let app_dir = app_dir();
        let static_dir = Some(app_dir.join("static").join("dist")).filter(|d| d.join("index.html").is_file());
        Ok(Self {
            bind,
            db_path: db_path()?,
            artifacts_dir,
            checkpoints_db,
            static_dir,
            kernel_python: python_in(&app_dir),
            work_dir: work_dir()?,
            app_dir: resolve(app_dir),
        })
    }
}

fn env_path(key: &str) -> Option<PathBuf> {
    std::env::var(key).ok().filter(|v| !v.is_empty()).map(PathBuf::from)
}

/// `Path.resolve()`: absolute, symlinks followed for whatever part exists.
fn resolve(path: PathBuf) -> PathBuf {
    let path = std::path::absolute(&path).unwrap_or(path);
    let mut existing = path.as_path();
    let mut rest = Vec::new();
    while !existing.exists() {
        match (existing.parent(), existing.file_name()) {
            (Some(parent), Some(name)) => {
                rest.push(name.to_os_string());
                existing = parent;
            }
            _ => return path,
        }
    }
    let mut out = std::fs::canonicalize(existing).unwrap_or_else(|_| existing.to_path_buf());
    out.extend(rest.iter().rev());
    out
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
    Ok(work_dir()?.join("database.db"))
}

/// `$WORK_DIR`, else `~/.jarvis`.
fn work_dir() -> Result<PathBuf, String> {
    match std::env::var("WORK_DIR") {
        Ok(dir) if !dir.is_empty() => Ok(PathBuf::from(dir)),
        _ => {
            let home = std::env::var("HOME").map_err(|_| "HOME is not set".to_string())?;
            Ok(PathBuf::from(home).join(".jarvis"))
        }
    }
}

/// The jarvis checkout: `JARVIS_APP_DIR`, else the working directory.
/// The interpreter jarvis's Python runs on — kernels, and code
/// automations (`sys.executable` in Python): `JARVIS_KERNEL_PYTHON`, else the
/// checkout's `.venv`, else `python3` on the PATH.
pub fn python() -> PathBuf {
    python_in(&app_dir())
}

fn python_in(app_dir: &Path) -> PathBuf {
    env_path("JARVIS_KERNEL_PYTHON").unwrap_or_else(|| {
        Some(app_dir.join(".venv").join("bin").join("python")).filter(|p| p.is_file()).unwrap_or_else(|| PathBuf::from("python3"))
    })
}

pub fn app_dir() -> PathBuf {
    env_path("JARVIS_APP_DIR").unwrap_or_else(|| std::env::current_dir().unwrap_or_default())
}
