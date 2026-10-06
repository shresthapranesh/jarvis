//! The agent's browser, as far as the edge reaches it: where it is, launching
//! one when nothing listens there (`tools/browser.py`'s "Reaching the
//! browser" — change both), and the live view of it (`screencast.rs`, `ws.rs`).
//!
//! The browser is its own process. The kernel drives it through Playwright;
//! the edge only finds it, starts it, and watches it.

pub mod cdp;
pub mod screencast;
pub mod ws;

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use sqlx::SqlitePool;

/// `DEFAULT_CDP_URL`.
pub const DEFAULT_CDP_URL: &str = "http://127.0.0.1:9222";
/// `_PROBE_TIMEOUT`.
const PROBE_TIMEOUT: Duration = Duration::from_secs(1);
/// `_LAUNCH_TIMEOUT`.
const LAUNCH_TIMEOUT: Duration = Duration::from_secs(25);

/// `_CANDIDATES`: the launch path's preference order.
#[cfg(target_os = "macos")]
const CANDIDATES: &[&str] = &[
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
];
#[cfg(target_os = "linux")]
const CANDIDATES: &[&str] = &[
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
];
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
const CANDIDATES: &[&str] = &[];

/// `_setting`: one `config_settings` value, stripped, a JSON-quoted one
/// unquoted; anything missing is "".
async fn setting(pool: &SqlitePool, key: &str) -> String {
    let stored = crate::catalog::setting(pool, key).await.ok().flatten().unwrap_or_default();
    let value = match serde_json::from_str::<serde_json::Value>(&stored) {
        Ok(serde_json::Value::String(s)) => s,
        _ => stored,
    };
    value.trim().to_string()
}

fn env(key: &str) -> String {
    std::env::var(key).unwrap_or_default().trim().to_string()
}

fn first(values: impl IntoIterator<Item = String>) -> Option<String> {
    values.into_iter().find(|v| !v.is_empty())
}

/// `cdp_url()`: the `browser.cdp_url` setting, else `JARVIS_BROWSER_CDP_URL`,
/// else the default port on loopback.
pub async fn cdp_url(pool: &SqlitePool) -> String {
    first([setting(pool, "browser.cdp_url").await, env("JARVIS_BROWSER_CDP_URL")])
        .unwrap_or_else(|| DEFAULT_CDP_URL.to_string())
}

/// `profile_dir()`: the dedicated user-data-dir, never the human's own.
async fn profile_dir(pool: &SqlitePool, work_dir: &Path) -> PathBuf {
    match first([setting(pool, "browser.profile_dir").await, env("JARVIS_BROWSER_PROFILE")]) {
        Some(configured) => expanduser(&configured),
        None => work_dir.join("browser-profile"),
    }
}

/// `executable()`: the browser binary for the launch path, or "" if none.
async fn executable(pool: &SqlitePool) -> String {
    if let Some(configured) = first([setting(pool, "browser.executable").await, env("JARVIS_BROWSER_EXECUTABLE")]) {
        return if Path::new(&configured).exists() || which(&configured).is_some() { configured } else { String::new() };
    }
    for candidate in CANDIDATES {
        if candidate.starts_with('/') {
            if Path::new(candidate).exists() {
                return candidate.to_string();
            }
        } else if let Some(found) = which(candidate) {
            return found.to_string_lossy().into_owned();
        }
    }
    String::new()
}

/// `shutil.which`, for a bare name or a path.
fn which(name: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let runnable = |p: &Path| p.is_file() && p.metadata().is_ok_and(|m| m.permissions().mode() & 0o111 != 0);
    if name.contains('/') {
        return Some(PathBuf::from(name)).filter(|p| runnable(p));
    }
    std::env::split_paths(&std::env::var_os("PATH")?).map(|dir| dir.join(name)).find(|p| runnable(p))
}

/// `Path.expanduser`, for the `~/` form.
fn expanduser(path: &str) -> PathBuf {
    match (path.strip_prefix("~/"), std::env::var("HOME")) {
        (Some(rest), Ok(home)) => PathBuf::from(home).join(rest),
        _ if path == "~" => std::env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from(path)),
        _ => PathBuf::from(path),
    }
}

/// `_has_display()`: macOS always has one; Linux needs a session.
fn has_display() -> bool {
    cfg!(target_os = "macos") || !env("DISPLAY").is_empty() || !env("WAYLAND_DISPLAY").is_empty()
}

/// `_endpoint_live(url)`.
pub async fn endpoint_live(http: &reqwest::Client, url: &str) -> bool {
    let probe = http.get(format!("{}/json/version", url.trim_end_matches('/'))).timeout(PROBE_TIMEOUT).send().await;
    probe.is_ok_and(|r| r.status() == reqwest::StatusCode::OK)
}

/// `_ensure_page(url)`: a browser whose last window was closed stays up with
/// no page target, and can't be attached to until it has one again.
pub async fn ensure_page(http: &reqwest::Client, url: &str) {
    let base = url.trim_end_matches('/');
    let listing = match http.get(format!("{base}/json/list")).timeout(PROBE_TIMEOUT).send().await {
        Ok(r) => r.json::<serde_json::Value>().await,
        Err(e) => {
            tracing::debug!("browser: could not ensure a page target: {e}");
            return;
        }
    };
    // Shape-checked, not trusted: `browser.cdp_url` can point at anything.
    let targets = match listing {
        Ok(serde_json::Value::Array(targets)) => targets,
        Ok(_) => {
            tracing::debug!("browser: /json/list returned something other than a list");
            return;
        }
        Err(e) => {
            tracing::debug!("browser: could not ensure a page target: {e}");
            return;
        }
    };
    if targets.iter().any(|t| t.get("type").and_then(|v| v.as_str()) == Some("page")) {
        return;
    }
    // PUT: Chromium made /json/new PUT-only.
    match http.put(format!("{base}/json/new?about:blank")).timeout(PROBE_TIMEOUT).send().await {
        Ok(_) => tracing::info!("browser: no page target, opened a blank tab"),
        Err(e) => tracing::debug!("browser: could not ensure a page target: {e}"),
    }
}

/// `launch()`: start a headed browser on the configured port; true if it
/// came up. Its own session, so it outlives whoever wanted it first.
async fn launch(pool: &SqlitePool, http: &reqwest::Client, work_dir: &Path) -> bool {
    let url = cdp_url(pool).await;
    let exe = executable(pool).await;
    if exe.is_empty() {
        return false;
    }
    if !has_display() {
        tracing::info!("browser: no display; set browser.cdp_url to a remote browser");
        return false;
    }
    let port = reqwest::Url::parse(&url).ok().and_then(|u| u.port()).unwrap_or(9222);
    let profile = profile_dir(pool, work_dir).await;
    if let Err(e) = std::fs::create_dir_all(&profile) {
        tracing::warn!("browser: launch failed ({exe}): {e}");
        return false;
    }
    let spawned = tokio::process::Command::new(&exe)
        .arg(format!("--remote-debugging-port={port}"))
        .arg(format!("--user-data-dir={}", profile.display()))
        // Brave and Edge open a welcome wizard on a fresh profile otherwise.
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .process_group(0)
        .spawn();
    match spawned {
        // Reaped when it exits; never killed by the edge.
        Ok(mut child) => drop(tokio::spawn(async move { child.wait().await })),
        Err(e) => {
            tracing::warn!("browser: launch failed ({exe}): {e}");
            return false;
        }
    }
    let deadline = Instant::now() + LAUNCH_TIMEOUT;
    while Instant::now() < deadline {
        if endpoint_live(http, &url).await {
            let name = Path::new(&exe).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or(exe.clone());
            tracing::info!("browser: launched {name} on {url}");
            return true;
        }
        tokio::time::sleep(Duration::from_millis(400)).await;
    }
    tracing::warn!("browser: {exe} did not open {url} within {}s", LAUNCH_TIMEOUT.as_secs_f64());
    false
}

/// `ensure_running()`: the CDP endpoint of a live browser, launching one if
/// needed; the error says which way it is unavailable, in Python's words.
pub async fn ensure_running(pool: &SqlitePool, http: &reqwest::Client, work_dir: &Path) -> Result<String, String> {
    let url = cdp_url(pool).await;
    if endpoint_live(http, &url).await || launch(pool, http, work_dir).await {
        ensure_page(http, &url).await;
        return Ok(url);
    }
    if executable(pool).await.is_empty() {
        return Err("no Chromium-based browser found — install one, or set the `browser.executable` config key to its path".into());
    }
    if !has_display() {
        return Err(
            "no display available for a headed browser; point `browser.cdp_url` at a browser running on a machine that has one"
                .into(),
        );
    }
    Err(format!("could not reach or start a browser at {url}"))
}
