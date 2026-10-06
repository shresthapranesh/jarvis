//! Live frames from the persistent browser, fanned out to whoever is
//! watching — a port of `core/browser_stream.py` (change both).
//!
//! CDP's `Page.startScreencast` is the whole source: Chrome encodes the JPEGs.
//! The edge is a second CDP client next to the kernel's Playwright one, on
//! the tab Playwright calls `pages[0]` (the first page `Target.setAutoAttach`
//! reports), so the panel shows the page the agent is on.
//!
//! The cast runs only while someone watches: the first subscriber attaches,
//! the last one detaches. Each subscriber sees only the newest frame (a
//! `watch` channel — depth 1, drop-oldest), so a slow socket drops frames
//! instead of holding up the browser.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use base64::Engine;
use serde_json::{Value, json};
use sqlx::SqlitePool;
use tokio::sync::{Mutex, mpsc, watch};

use super::cdp::{Cdp, Event};

/// `FRAME_FORMAT`, `FRAME_QUALITY`, `FRAME_MAX_WIDTH`, `FRAME_MAX_HEIGHT`,
/// `EVERY_NTH_FRAME`.
const FRAME_FORMAT: &str = "jpeg";
const FRAME_QUALITY: u32 = 60;
const FRAME_MAX_WIDTH: u32 = 1280;
const FRAME_MAX_HEIGHT: u32 = 800;
const EVERY_NTH_FRAME: u32 = 2;
/// `_ATTACH_TIMEOUT`.
const ATTACH_TIMEOUT: Duration = Duration::from_secs(15);

/// One frame plus what the client needs to place it.
pub struct Frame {
    pub data: bytes::Bytes,
    pub width: i64,
    pub height: i64,
    pub url: String,
}

/// Newest frame; the sender's end closes when the cast is gone.
pub type Frames = watch::Receiver<Option<Arc<Frame>>>;

pub struct Screencast {
    pool: SqlitePool,
    http: reqwest::Client,
    work_dir: PathBuf,
    inner: Mutex<Inner>,
}

#[derive(Default)]
struct Inner {
    subscribers: usize,
    cast: Option<Cast>,
}

struct Cast {
    cdp: Arc<Cdp>,
    session: String,
    frames: Frames,
}

/// A viewer's hold on the cast; dropping it lets the cast stop.
pub struct Subscription {
    pub frames: Frames,
    hub: Arc<Screencast>,
}

impl Drop for Subscription {
    fn drop(&mut self) {
        let hub = self.hub.clone();
        tokio::spawn(async move { hub.release().await });
    }
}

impl Screencast {
    pub fn new(pool: SqlitePool, http: reqwest::Client, work_dir: PathBuf) -> Arc<Self> {
        Arc::new(Screencast { pool, http, work_dir, inner: Default::default() })
    }

    /// `subscribe()`. Attaching happens under the lock, so two sockets opening
    /// at once can't race two CDP connections into existence. A late joiner
    /// starts from the last frame rather than waiting for the next paint.
    pub async fn subscribe(self: &Arc<Self>) -> Result<Subscription, String> {
        let mut inner = self.inner.lock().await;
        // A cast whose browser went away is started afresh.
        if inner.cast.as_ref().is_some_and(|c| c.frames.has_changed().is_err()) {
            detach(inner.cast.take());
        }
        if inner.cast.is_none() {
            let cast = match tokio::time::timeout(ATTACH_TIMEOUT, self.attach()).await {
                Ok(Ok(cast)) => cast,
                Ok(Err(e)) => return Err(e),
                Err(_) => return Err(format!("no browser attached within {}s", ATTACH_TIMEOUT.as_secs())),
            };
            inner.cast = Some(cast);
        }
        inner.subscribers += 1;
        let frames = inner.cast.as_ref().expect("a cast").frames.clone();
        Ok(Subscription { frames, hub: self.clone() })
    }

    async fn release(&self) {
        let mut inner = self.inner.lock().await;
        inner.subscribers = inner.subscribers.saturating_sub(1);
        if inner.subscribers == 0 {
            detach(inner.cast.take());
        }
    }

    /// `_attach`: find (or start) the browser, attach to the agent's tab and
    /// start the cast.
    async fn attach(&self) -> Result<Cast, String> {
        let endpoint = super::ensure_running(&self.pool, &self.http, &self.work_dir).await?;
        let (cdp, mut events) = Cdp::connect(&self.http, &endpoint).await?;
        let cdp = Arc::new(cdp);
        let started = start(&cdp, &mut events).await;
        let (session, main_frame, url) = match started {
            Ok(found) => found,
            Err(e) => {
                cdp.close();
                return Err(e);
            }
        };
        tracing::info!("browser stream: casting from {endpoint}");
        let (tx, frames) = watch::channel(None);
        if let Some(frame) = prime(&cdp, &session, &url).await {
            tx.send_replace(Some(Arc::new(frame)));
        }
        tokio::spawn(pump(cdp.clone(), events, session.clone(), main_frame, url, tx));
        Ok(Cast { cdp, session, frames })
    }
}

/// `_detach`: stop the cast and drop the connection. The browser carries on.
fn detach(cast: Option<Cast>) {
    if let Some(cast) = cast {
        cast.cdp.send(Some(&cast.session), "Page.stopScreencast", json!({}));
        cast.cdp.close();
        tracing::info!("browser stream: stopped");
    }
}

/// The tab, as `ctx.pages[0] if ctx.pages else ctx.new_page()` finds it, with
/// its page events on and the cast started: (session, main frame, url).
async fn start(cdp: &Cdp, events: &mut mpsc::UnboundedReceiver<Event>) -> Result<(String, String, String), String> {
    // Every existing target is reported before the reply, in Playwright's order.
    cdp.call(None, "Target.setAutoAttach", json!({"autoAttach": true, "waitForDebuggerOnStart": false, "flatten": true}))
        .await?;
    let mut session = None;
    while let Ok(ev) = events.try_recv() {
        if session.is_none() && ev.method == "Target.attachedToTarget" && ev.params["targetInfo"]["type"] == "page" {
            session = ev.params["sessionId"].as_str().map(str::to_string);
        }
    }
    let session = match session {
        Some(s) => s,
        None => {
            let created = cdp.call(None, "Target.createTarget", json!({"url": "about:blank"})).await?;
            let target = created["targetId"].clone();
            loop {
                let ev = events.recv().await.ok_or("the browser connection closed")?;
                if ev.method == "Target.attachedToTarget" && ev.params["targetInfo"]["targetId"] == target {
                    break ev.params["sessionId"].as_str().unwrap_or_default().to_string();
                }
            }
        }
    };
    let page = Some(session.as_str());
    cdp.call(page, "Page.enable", json!({})).await?;
    let tree = cdp.call(page, "Page.getFrameTree", json!({})).await?;
    let frame = &tree["frameTree"]["frame"];
    let main_frame = frame["id"].as_str().unwrap_or_default().to_string();
    let url = frame_url(frame);
    cdp.call(
        page,
        "Page.startScreencast",
        json!({
            "format": FRAME_FORMAT,
            "quality": FRAME_QUALITY,
            "maxWidth": FRAME_MAX_WIDTH,
            "maxHeight": FRAME_MAX_HEIGHT,
            "everyNthFrame": EVERY_NTH_FRAME,
        }),
    )
    .await?;
    Ok((session, main_frame, url))
}

/// Playwright's `frame.url`: the URL plus its fragment.
fn frame_url(frame: &Value) -> String {
    format!("{}{}", frame["url"].as_str().unwrap_or_default(), frame["urlFragment"].as_str().unwrap_or_default())
}

/// `_prime`: one screenshot now, since the screencast only emits on paint
/// and a page sitting still would otherwise show nothing at all.
async fn prime(cdp: &Cdp, session: &str, url: &str) -> Option<Frame> {
    let page = Some(session);
    let captured = async {
        let shot = cdp.call(page, "Page.captureScreenshot", json!({"format": FRAME_FORMAT, "quality": FRAME_QUALITY})).await?;
        let metrics = cdp.call(page, "Page.getLayoutMetrics", json!({})).await?;
        Ok::<_, String>((shot, metrics))
    };
    let (shot, metrics) = match captured.await {
        Ok(both) => both,
        Err(e) => {
            tracing::debug!("browser stream: priming frame failed: {e}");
            return None;
        }
    };
    let data = base64::engine::general_purpose::STANDARD.decode(shot["data"].as_str()?).ok()?.into();
    let viewport = &metrics["cssVisualViewport"];
    Some(Frame { data, width: int(&viewport["clientWidth"]), height: int(&viewport["clientHeight"]), url: url.to_string() })
}

/// `int(x or 0)`.
fn int(value: &Value) -> i64 {
    value.as_f64().map_or(0, |x| x.trunc() as i64)
}

/// The tab's events, until it or the browser goes away: frames out (each
/// acked first — Chrome stops casting until it is), the URL kept current as
/// Playwright keeps `page.url`.
async fn pump(
    cdp: Arc<Cdp>,
    mut events: mpsc::UnboundedReceiver<Event>,
    session: String,
    mut main_frame: String,
    mut url: String,
    frames: watch::Sender<Option<Arc<Frame>>>,
) {
    while let Some(ev) = events.recv().await {
        if ev.method == "Target.detachedFromTarget" && ev.params["sessionId"] == session.as_str() {
            break;
        }
        if ev.session != session {
            continue;
        }
        match ev.method.as_str() {
            "Page.screencastFrame" => {
                cdp.send(Some(&session), "Page.screencastFrameAck", json!({"sessionId": ev.params["sessionId"]}));
                let Some(data) =
                    ev.params["data"].as_str().and_then(|d| base64::engine::general_purpose::STANDARD.decode(d).ok())
                else {
                    continue;
                };
                let meta = &ev.params["metadata"];
                let frame = Frame { data: data.into(), width: int(&meta["deviceWidth"]), height: int(&meta["deviceHeight"]), url: url.clone() };
                frames.send_replace(Some(Arc::new(frame)));
            }
            "Page.frameNavigated" => {
                let frame = &ev.params["frame"];
                if frame.get("parentId").is_none_or(Value::is_null) {
                    main_frame = frame["id"].as_str().unwrap_or_default().to_string();
                    url = frame_url(frame);
                }
            }
            "Page.navigatedWithinDocument" if ev.params["frameId"] == main_frame.as_str() => {
                url = ev.params["url"].as_str().unwrap_or_default().to_string();
            }
            _ => {}
        }
    }
    // `frames` drops here: every viewer learns the cast is gone.
}
