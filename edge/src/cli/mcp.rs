//! `mcp add|list|get|remove` — the `mcp.servers` setting, as the MCP tab
//! writes it (`gql/mcp.rs`). Header and env values print as `••••`, as the
//! UI shows them. A running server is told to reconnect after a write and
//! asked which tools each server has; with none running, a write waits for
//! the next start.

use std::io::{BufRead, IsTerminal, Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

use clap::Subcommand;
use serde_json::{Map, Value, json};
use sqlx::SqlitePool;

use super::{Done, Fail, bold, dim, ok, table, yellow};
use crate::config::Config;
use crate::mcp::config::{self, Connection};
use crate::pyjson;

const TRANSPORTS: [&str; 4] = ["http", "streamable-http", "sse", "websocket"];

#[derive(Subcommand)]
pub enum Cmd {
    /// Add a server: a URL, or a command to run after `--`.
    #[command(after_help = "Examples:\n  \
        jarvis-edge mcp add booking https://example.com/mcp --token -\n  \
        jarvis-edge mcp add github -e GITHUB_PERSONAL_ACCESS_TOKEN=… -- npx -y @modelcontextprotocol/server-github")]
    Add {
        /// A name for the server.
        name: String,
        /// The server's URL.
        url: Option<String>,
        /// How to reach the URL: http (default), streamable-http, sse or websocket.
        #[arg(long, short)]
        transport: Option<String>,
        /// A bearer token, sent as `Authorization: Bearer …`. `-` asks for it
        /// without echoing it (or reads a pipe), keeping it out of shell history.
        #[arg(long)]
        token: Option<String>,
        /// A request header, `Name: value`. Repeatable.
        #[arg(long = "header", short = 'H', value_name = "HEADER")]
        headers: Vec<String>,
        /// An environment variable for a command, `KEY=value`. Repeatable.
        #[arg(long = "env", short = 'e', value_name = "KEY=VALUE")]
        env: Vec<String>,
        /// Load its tools on demand instead of binding them to the agent.
        #[arg(long)]
        lazy: bool,
        /// The command that runs the server, and its arguments.
        #[arg(last = true, value_name = "COMMAND")]
        command: Vec<String>,
    },
    /// List the configured servers.
    List,
    /// Show one server's config and tools.
    Get {
        /// The server's name.
        name: String,
    },
    /// Remove a server added here or in the UI.
    Remove {
        /// The server's name.
        name: String,
    },
}

pub async fn run(cfg: &Config, pool: &SqlitePool, cmd: Cmd) -> Done {
    match cmd {
        Cmd::Add { name, url, transport, token, headers, env, lazy, command } => {
            let name = crate::pystr::strip(&name).to_string();
            if name.is_empty() {
                return Err(error("the name is empty"));
            }
            let conn = connection(url, transport, token, &headers, &env, lazy, command)?;
            if config::merged(pool, &cfg.app_dir).await.contains_key(&name) {
                return Err(error(format!("MCP server {name:?} already exists — `jarvis-edge mcp remove {name}` first")));
            }
            crate::gql::mcp::add_to_db(pool, &name, &Value::Object(conn)).await.map_err(|e| error(e.message))?;
            println!("{}", ok(&format!("Added MCP server {name}")));
            reconnect(cfg.bind, Some(&name)).await;
        }
        Cmd::List => {
            let servers = config::merged(pool, &cfg.app_dir).await;
            if servers.is_empty() {
                println!("{}", yellow("No MCP servers configured."));
                return Ok(0);
            }
            let saved = config::from_db(pool).await;
            let default = config::default_mode(pool).await;
            let live = ask(cfg.bind, "{ mcpServers { name toolCount } }", json!({})).await;
            let count = |name: &str| -> String {
                let Some(data) = &live else { return "—".into() };
                let mut servers = data["mcpServers"].as_array().into_iter().flatten();
                servers.find(|s| s["name"] == name).map_or("—".into(), |s| s["toolCount"].to_string())
            };
            let rows: Vec<Vec<String>> = servers
                .iter()
                .map(|(name, c)| {
                    let from = if saved.contains_key(name) { "settings" } else { "file/env" };
                    vec![name.clone(), transport(c), target(c), config::mode_for(Some(c), default).into(), count(name), from.into()]
                })
                .collect();
            table("MCP Servers", &["Name", "Transport", "Target", "Load", "Tools", "From"], &rows);
            if live.is_none() {
                println!("{}", dim("The server isn't running, so tools aren't listed."));
            }
        }
        Cmd::Get { name } => {
            let servers = config::merged(pool, &cfg.app_dir).await;
            let Some(c) = servers.get(&name) else { return Err(not_found(&name)) };
            let shown = Value::Object(config::strip_jarvis_keys(&config::redact(c)));
            println!("{}", bold(&name));
            println!("{}", serde_json::to_string_pretty(&shown).unwrap_or_default());
            println!("Load: {}", config::mode_for(Some(c), config::default_mode(pool).await));
            let from = if config::from_db(pool).await.contains_key(&name) { "settings" } else { "mcp.json or JARVIS_MCP_SERVERS" };
            println!("From: {from}");
            let q = "query($s: String) { mcpTools(server: $s) { name } }";
            match ask(cfg.bind, q, json!({"s": name})).await {
                None => println!("{}", dim("Tools: the server isn't running.")),
                Some(data) => {
                    let tools: Vec<&str> = data["mcpTools"].as_array().into_iter().flatten().filter_map(|t| t["name"].as_str()).collect();
                    if tools.is_empty() {
                        println!("Tools: {}", yellow("none — not connected? The server's log has why."));
                    } else {
                        println!("Tools ({}): {}", tools.len(), tools.join(", "));
                    }
                }
            }
        }
        Cmd::Remove { name } => {
            let mut saved = config::from_db(pool).await;
            if saved.shift_remove(&name).is_none() {
                if config::merged(pool, &cfg.app_dir).await.contains_key(&name) {
                    return Err(error(format!("{name:?} comes from mcp.json or JARVIS_MCP_SERVERS — remove it there")));
                }
                return Err(not_found(&name));
            }
            let value = pyjson::dumps(&serde_json::to_value(&saved).map_err(|e| error(e.to_string()))?);
            crate::gql::mcp::write_setting(pool, config::SERVERS_KEY, &value).await.map_err(|e| error(e.message))?;
            println!("{}", ok(&format!("Removed MCP server {name}")));
            reconnect(cfg.bind, None).await;
        }
    }
    Ok(0)
}

fn error(message: impl Into<String>) -> Fail {
    Fail::Error(message.into())
}

fn not_found(name: &str) -> Fail {
    error(format!("No MCP server named {name:?} — `jarvis-edge mcp list` shows them"))
}

/// The connection dict `add` saves, as the UI's editor writes one.
fn connection(
    url: Option<String>,
    transport: Option<String>,
    token: Option<String>,
    headers: &[String],
    env: &[String],
    lazy: bool,
    command: Vec<String>,
) -> Result<Connection, Fail> {
    let mut out = Connection::new();
    match (url, command.is_empty()) {
        (Some(_), false) => return Err(error("give a URL or a command after `--`, not both")),
        (None, true) => return Err(error("give the server's URL, or the command that runs it after `--`")),
        (None, false) => {
            if transport.as_deref().is_some_and(|t| t != "stdio") {
                return Err(error("a command runs over stdio; --transport is for a URL"));
            }
            if token.is_some() || !headers.is_empty() {
                return Err(error("--token and --header are for a URL; a command takes --env"));
            }
            out.insert("transport".into(), "stdio".into());
            out.insert("command".into(), command[0].clone().into());
            if command.len() > 1 {
                out.insert("args".into(), command[1..].iter().cloned().map(Value::String).collect());
            }
            let vars = pairs(env, '=', "--env", "KEY=value")?;
            if !vars.is_empty() {
                out.insert("env".into(), Value::Object(vars));
            }
        }
        (Some(url), true) => {
            let transport = transport.unwrap_or_else(|| "http".into());
            if !TRANSPORTS.contains(&transport.as_str()) {
                return Err(error(format!("--transport must be one of {}", TRANSPORTS.join(", "))));
            }
            if !env.is_empty() {
                return Err(error("--env is for a command; a URL takes --header or --token"));
            }
            reqwest::Url::parse(&url).map_err(|e| error(format!("invalid URL {url:?}: {e}")))?;
            out.insert("transport".into(), transport.into());
            out.insert("url".into(), url.into());
            let mut hdrs = pairs(headers, ':', "--header", "Name: value")?;
            if let Some(token) = token {
                if hdrs.keys().any(|k| k.eq_ignore_ascii_case("authorization")) {
                    return Err(error("--token sets the Authorization header; don't give it with --header too"));
                }
                let token = if token == "-" { read_secret("Token: ").map_err(|e| error(format!("reading the token: {e}")))? } else { token };
                let token = token.trim();
                if token.is_empty() {
                    return Err(error("the token is empty"));
                }
                // A bare token is a bearer token; one with a scheme ("Basic …") goes as written.
                let value = if token.contains(char::is_whitespace) { token.to_string() } else { format!("Bearer {token}") };
                hdrs.insert("Authorization".into(), value.into());
            }
            if !hdrs.is_empty() {
                out.insert("headers".into(), Value::Object(hdrs));
            }
        }
    }
    if lazy {
        out.insert(config::LOAD_MODE_KEY.into(), config::LAZY.into());
    }
    Ok(out)
}

/// `KEY<sep>value` arguments as a map, in the order given.
fn pairs(args: &[String], sep: char, flag: &str, shape: &str) -> Result<Map<String, Value>, Fail> {
    let mut out = Map::new();
    for arg in args {
        let Some((k, v)) = arg.split_once(sep).filter(|(k, _)| !k.trim().is_empty()) else {
            return Err(error(format!("{flag} takes {shape}, not {arg:?}")));
        };
        out.insert(k.trim().into(), v.trim().into());
    }
    Ok(out)
}

/// A line read without echoing it, or all of a pipe.
fn read_secret(prompt: &str) -> std::io::Result<String> {
    let stdin = std::io::stdin();
    if !stdin.is_terminal() {
        let mut s = String::new();
        stdin.lock().read_to_string(&mut s)?;
        return Ok(s);
    }
    eprint!("{prompt}");
    std::io::stderr().flush()?;
    let fd = libc::STDIN_FILENO;
    // SAFETY: termios is plain data, filled by tcgetattr before it's used.
    let mut term: libc::termios = unsafe { std::mem::zeroed() };
    let quiet = unsafe { libc::tcgetattr(fd, &mut term) } == 0;
    let original = term;
    if quiet {
        term.c_lflag &= !libc::ECHO;
        unsafe { libc::tcsetattr(fd, libc::TCSANOW, &term) };
    }
    let mut line = String::new();
    let read = stdin.lock().read_line(&mut line);
    if quiet {
        unsafe { libc::tcsetattr(fd, libc::TCSANOW, &original) };
    }
    eprintln!();
    read.map(|_| line)
}

fn transport(c: &Connection) -> String {
    c.get("transport").map(pyjson::py_str).unwrap_or_else(|| if c.contains_key("command") { "stdio" } else { "http" }.into())
}

fn target(c: &Connection) -> String {
    if let Some(url) = c.get("url").filter(|u| pyjson::truthy(u)) {
        return pyjson::py_str(url);
    }
    let args = c.get("args").and_then(Value::as_array).into_iter().flatten().map(pyjson::py_str);
    c.get("command").map(pyjson::py_str).into_iter().chain(args).collect::<Vec<_>>().join(" ")
}

/// The running server's `data` for a query, or `None` when none answers.
async fn ask(bind: SocketAddr, query: &str, variables: Value) -> Option<Value> {
    let ip = match bind.ip() {
        IpAddr::V4(ip) if ip.is_unspecified() => IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(ip) if ip.is_unspecified() => IpAddr::V6(Ipv6Addr::LOCALHOST),
        ip => ip,
    };
    let url = format!("http://{}/graphql", SocketAddr::new(ip, bind.port()));
    // Reconnecting waits on every server's connect.
    let client = reqwest::Client::builder().connect_timeout(Duration::from_secs(2)).timeout(Duration::from_secs(180)).build().ok()?;
    let resp = client.post(url).json(&json!({"query": query, "variables": variables})).send().await.ok()?;
    resp.json::<Value>().await.ok()?.get("data").filter(|d| d.is_object()).cloned()
}

/// Tell a running server the config changed, and say how `name` came up.
async fn reconnect(bind: SocketAddr, name: Option<&str>) {
    let Some(data) = ask(bind, "mutation { reloadMcpServers { name toolCount } }", json!({})).await else {
        println!("{}", dim("The server isn't running; it connects when it starts."));
        return;
    };
    let Some(name) = name else { return };
    let tools = data["reloadMcpServers"].as_array().into_iter().flatten().find(|s| s["name"] == name).and_then(|s| s["toolCount"].as_i64());
    match tools {
        Some(1) => println!("{}", ok("Connected — 1 tool")),
        Some(n) if n > 1 => println!("{}", ok(&format!("Connected — {n} tools"))),
        _ => println!("{}", yellow("Not connected — the server's log has why. `jarvis-edge mcp remove` and add it again to fix it.")),
    }
}
