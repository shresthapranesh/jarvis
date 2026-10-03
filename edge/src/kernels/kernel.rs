//! One `ipykernel` process and the client end of its sockets — what
//! `jupyter_client`'s `AsyncKernelManager` + client did for `core/kernels.py`.
//!
//! - Launched as `python -m ipykernel_launcher -f <connection file>` over
//!   `ipc` (unix sockets, no TCP), each kernel at its own socket path, in
//!   its own process group so an interrupt reaches what it spawned too.
//! - `JPY_PARENT_PID` makes the kernel exit if the edge dies without
//!   stopping it.
//! - Each socket has a reader task that forwards parsed messages to a
//!   channel. A timeout around a channel receive drops nothing, where one
//!   around a socket read could cut a message in half.

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::json;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use zeromq::{DealerSendHalf, Socket, SocketRecv, SocketSend, ZmqMessage};

use super::wire::{Msg, Signer};

/// How to start a kernel.
#[derive(Clone, Debug)]
pub struct Launch {
    /// The interpreter whose packages the kernel sees (the jarvis venv).
    pub python: PathBuf,
    /// The kernel's working directory: the jarvis checkout.
    pub dir: PathBuf,
    /// Set in the kernel's environment, over the edge's own.
    pub env: Vec<(String, String)>,
}

const PORTS: [(&str, u16); 5] =
    [("shell_port", 1), ("iopub_port", 2), ("stdin_port", 3), ("control_port", 4), ("hb_port", 5)];
const SHELL: u16 = 1;
const IOPUB: u16 = 2;

pub struct Kernel {
    child: tokio::process::Child,
    /// The kernel's pid, and its process group's.
    pid: i32,
    signer: Signer,
    shell: DealerSendHalf,
    replies: mpsc::UnboundedReceiver<Msg>,
    iopub: mpsc::UnboundedReceiver<Msg>,
    readers: Vec<JoinHandle<()>>,
    /// The socket path prefix; the connection file and sockets hang off it.
    base: PathBuf,
}

impl Kernel {
    /// Start a kernel and wait until it answers on both shell and iopub,
    /// or fail after `within`.
    pub async fn start(launch: &Launch, within: Duration) -> Result<Kernel, String> {
        // Short and unique: a unix socket path caps at ~104 bytes, and
        // kernels starting together must not pick the same endpoints.
        let id = uuid::Uuid::new_v4().simple().to_string();
        let base = std::env::temp_dir().join(format!("jarvis-kernel-{}", &id[..12]));
        let key = uuid::Uuid::new_v4().to_string();
        let mut info = json!({
            "ip": base.to_string_lossy(),
            "key": key,
            "transport": "ipc",
            "signature_scheme": "hmac-sha256",
            "kernel_name": "",
        });
        for (name, port) in PORTS {
            info[name] = port.into();
        }
        let file = connection_file(&base);
        std::fs::write(&file, info.to_string()).map_err(|e| format!("connection file {}: {e}", file.display()))?;

        let mut cmd = tokio::process::Command::new(&launch.python);
        cmd.args(["-m", "ipykernel_launcher", "-f"])
            .arg(&file)
            .current_dir(&launch.dir)
            .env("JPY_PARENT_PID", std::process::id().to_string())
            .envs(launch.env.iter().map(|(k, v)| (k, v)))
            .stdin(std::process::Stdio::null())
            .process_group(0)
            .kill_on_drop(true);
        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                remove_files(&base);
                return Err(format!("starting {}: {e}", launch.python.display()));
            }
        };
        let pid = child.id().map_or(0, |p| p as i32);
        let signer = Signer::new(&key);
        let mut kernel = match tokio::time::timeout(within, connect(&base, &signer)).await {
            Ok(Ok((shell, replies, iopub, readers))) => {
                Kernel { child, pid, signer, shell, replies, iopub, readers, base }
            }
            failed => {
                let mut k = Detached { child, pid, base };
                let why = match failed {
                    Ok(Err(e)) => e,
                    _ => "the kernel's sockets never came up".into(),
                };
                return Err(k.kill(why).await);
            }
        };
        match tokio::time::timeout(within, kernel.wait_for_ready()).await {
            Ok(Ok(())) => Ok(kernel),
            failed => {
                let why = match failed {
                    Ok(Err(e)) => e,
                    _ => format!("the kernel wasn't ready within {}s", within.as_secs()),
                };
                kernel.shutdown().await;
                Err(why)
            }
        }
    }

    /// `kernel_info_request` until one is answered on shell *and* its status
    /// shows on iopub: a subscriber that connected late misses what was
    /// published before, so a reply alone doesn't prove output will arrive.
    async fn wait_for_ready(&mut self) -> Result<(), String> {
        loop {
            if !self.alive() {
                return Err("the kernel exited while starting".into());
            }
            let (id, msg) = self.signer.request("kernel_info_request", json!({}));
            self.shell.send(msg).await.map_err(|e| format!("kernel shell: {e}"))?;
            let replied = tokio::time::timeout(Duration::from_secs(2), async {
                while let Some(m) = self.replies.recv().await {
                    if m.parent_id.as_deref() == Some(&id) {
                        return true;
                    }
                }
                false
            })
            .await;
            if !matches!(replied, Ok(true)) {
                continue;
            }
            let published = tokio::time::timeout(Duration::from_secs(1), async {
                while let Some(m) = self.iopub.recv().await {
                    if m.parent_id.as_deref() == Some(&id) {
                        return true;
                    }
                }
                false
            })
            .await;
            if matches!(published, Ok(true)) {
                return Ok(());
            }
        }
    }

    /// Send a cell; returns its `msg_id`. Never asks for input: a cell that
    /// calls `input()` fails at once instead of waiting out its timeout.
    pub async fn execute(&mut self, code: &str, silent: bool) -> Result<String, String> {
        // Nothing reads execute replies — the cell's output and its end come
        // on iopub — so don't let them pile up.
        while self.replies.try_recv().is_ok() {}
        let (id, msg) = self.signer.request(
            "execute_request",
            json!({
                "code": code,
                "silent": silent,
                "store_history": !silent,
                "user_expressions": {},
                "allow_stdin": false,
                "stop_on_error": true,
            }),
        );
        self.shell.send(msg).await.map_err(|e| format!("kernel shell: {e}"))?;
        Ok(id)
    }

    /// The next output message. Cancel-safe.
    pub async fn next_output(&mut self) -> Option<Msg> {
        self.iopub.recv().await
    }

    pub fn alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// The kernel's pid, for an interrupt from outside its lock.
    pub fn pid(&self) -> i32 {
        self.pid
    }

    /// Kill it and remove its files. Variables are gone.
    pub async fn shutdown(mut self) {
        for r in &self.readers {
            r.abort();
        }
        Detached { child: self.child, pid: self.pid, base: std::mem::take(&mut self.base) }.kill(String::new()).await;
    }
}

/// SIGINT to the kernel's process group: stops the running cell and keeps
/// the session, like a notebook's interrupt button.
pub fn interrupt(pid: i32) {
    if pid > 0 {
        // SAFETY: plain syscall on a process group the edge started.
        unsafe { libc::killpg(pid, libc::SIGINT) };
    }
}

type Connected = (DealerSendHalf, mpsc::UnboundedReceiver<Msg>, mpsc::UnboundedReceiver<Msg>, Vec<JoinHandle<()>>);

async fn connect(base: &Path, signer: &Signer) -> Result<Connected, String> {
    let endpoint = |port: u16| format!("ipc://{}-{port}", base.display());
    // `connect` retries until the kernel has bound the socket.
    let mut shell = zeromq::DealerSocket::new();
    shell.connect(&endpoint(SHELL)).await.map_err(|e| format!("kernel shell: {e}"))?;
    let mut iopub = zeromq::SubSocket::new();
    iopub.subscribe("").await.map_err(|e| format!("kernel iopub: {e}"))?;
    iopub.connect(&endpoint(IOPUB)).await.map_err(|e| format!("kernel iopub: {e}"))?;

    let (shell, shell_rx) = shell.split();
    let (reply_tx, replies) = mpsc::unbounded_channel();
    let (out_tx, outputs) = mpsc::unbounded_channel();
    let readers = vec![forward(shell_rx, signer.clone(), reply_tx), forward(iopub, signer.clone(), out_tx)];
    Ok((shell, replies, outputs, readers))
}

fn forward<S: SocketRecv + Send + 'static>(mut socket: S, signer: Signer, tx: mpsc::UnboundedSender<Msg>) -> JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            let msg: ZmqMessage = match socket.recv().await {
                Ok(m) => m,
                Err(_) => return,
            };
            if let Some(m) = signer.parse(msg)
                && tx.send(m).is_err()
            {
                return;
            }
        }
    })
}

fn connection_file(base: &Path) -> PathBuf {
    let mut f = base.as_os_str().to_owned();
    f.push(".json");
    PathBuf::from(f)
}

/// The kernel's connection file and socket files (`cleanup_connection_file`,
/// `cleanup_ipc_files`).
fn remove_files(base: &Path) {
    if base.as_os_str().is_empty() {
        return;
    }
    let _ = std::fs::remove_file(connection_file(base));
    for (_, port) in PORTS {
        let mut f = base.as_os_str().to_owned();
        f.push(format!("-{port}"));
        let _ = std::fs::remove_file(f);
    }
}

/// A kernel process without its client.
struct Detached {
    child: tokio::process::Child,
    pid: i32,
    base: PathBuf,
}

impl Detached {
    /// Kill the group, reap the process, remove the files; hands `why` back.
    async fn kill(&mut self, why: String) -> String {
        if self.pid > 0 {
            // SAFETY: as in `interrupt`.
            unsafe { libc::killpg(self.pid, libc::SIGKILL) };
        }
        let _ = self.child.kill().await;
        remove_files(&self.base);
        why
    }
}
