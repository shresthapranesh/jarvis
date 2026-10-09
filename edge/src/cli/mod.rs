//! The command line — a port of `main.py` (change both): `run`, `start`,
//! `config *`, `model *`, `memory *`; `mcp *` is the edge's own. No subcommand serves, as the edge
//! always has.
//!
//! The commands read and write the database through the server's own code
//! (`catalog.rs`, `gql/models.rs`, `discovery.rs`, the agent loop).
//!
//! The output says what `main.py`'s says, as plain text: tables are aligned
//! columns, a report is printed as its Markdown.

mod config;
mod mcp;
mod memory;
mod model;
mod run;

use std::io::IsTerminal;
use std::path::PathBuf;

use clap::{Parser, Subcommand};
use sqlx::SqlitePool;

#[derive(Parser)]
#[command(name = "jarvis-edge", about = "General-purpose research and analysis agent. With no command, serves.")]
struct Cli {
    /// Working directory for databases and memory files (`WORK_DIR`).
    #[arg(long, global = true)]
    work_dir: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
pub enum Command {
    /// Run the research agent on a query and display the report.
    Run {
        /// The research question or topic to investigate.
        query: String,
        /// Model identifier to use (default: the configured default).
        #[arg(long)]
        model: Option<String>,
        /// Log to the console at DEBUG level.
        #[arg(long)]
        debug: bool,
        /// Do not save the report to disk.
        #[arg(long)]
        no_save: bool,
    },
    /// Start the server.
    Start {
        /// Bind address (default 127.0.0.1).
        #[arg(long)]
        host: Option<String>,
        /// TCP port (default 8000).
        #[arg(long)]
        port: Option<u16>,
        /// Log at DEBUG level.
        #[arg(long)]
        debug: bool,
    },
    /// Manage persistent configuration.
    #[command(subcommand)]
    Config(config::Cmd),
    /// Manage models.
    #[command(subcommand)]
    Model(model::Cmd),
    /// Manage agent memory (AGENTS.md in the key-value store).
    #[command(subcommand)]
    Memory(memory::Cmd),
    /// Manage MCP servers.
    #[command(subcommand)]
    Mcp(mcp::Cmd),
}

/// What the process is for.
pub enum Mode {
    Serve,
    Cli(Command),
}

/// Parse the command line. Run before the runtime starts: `--work-dir` and
/// `start`'s options become the environment the rest of the edge reads, and
/// no other thread exists yet to read it concurrently.
pub fn parse() -> Mode {
    let cli = Cli::parse();
    let set = |key: &str, value: &str| {
        // SAFETY: single-threaded — the runtime hasn't been built.
        unsafe { std::env::set_var(key, value) }
    };
    if let Some(dir) = &cli.work_dir {
        set("WORK_DIR", &dir.to_string_lossy());
    }
    match cli.command {
        None => Mode::Serve,
        Some(Command::Start { host, port, debug }) => {
            if host.is_some() || port.is_some() {
                let current = std::env::var("JARVIS_EDGE_BIND").ok().and_then(|b| b.parse::<std::net::SocketAddr>().ok());
                let host = host.unwrap_or_else(|| current.map_or("127.0.0.1".into(), |a| a.ip().to_string()));
                let port = port.unwrap_or_else(|| current.map_or(8000, |a| a.port()));
                let host = if host.contains(':') && !host.starts_with('[') { format!("[{host}]") } else { host };
                set("JARVIS_EDGE_BIND", &format!("{host}:{port}"));
            }
            if debug {
                set("JARVIS_EDGE_LOG", "debug");
            }
            Mode::Serve
        }
        Some(command) => Mode::Cli(command),
    }
}

/// Why a command didn't finish.
pub enum Fail {
    /// A failure to report.
    Error(String),
}

impl From<sqlx::Error> for Fail {
    fn from(e: sqlx::Error) -> Self {
        Fail::Error(format!("database: {e}"))
    }
}

/// A command's exit code.
type Done = Result<i32, Fail>;

/// Run a command; never returns.
pub async fn main(command: Command) -> ! {
    let debug = matches!(command, Command::Run { debug: true, .. });
    logging(debug);
    let code = match dispatch(command).await {
        Ok(code) => code,
        Err(Fail::Error(e)) => {
            eprintln!("{} {e}", red("Error:"));
            1
        }
    };
    std::process::exit(code)
}

async fn dispatch(command: Command) -> Done {
    let (config, pool) = open().await?;
    match command {
        Command::Run { query, model, no_save, .. } => run::run(&config, &pool, query, model, no_save).await,
        Command::Config(cmd) => config::run(&pool, cmd).await,
        Command::Model(cmd) => model::run(&pool, cmd).await,
        Command::Memory(cmd) => memory::run(&pool, &config.checkpoints_db, cmd).await,
        Command::Mcp(cmd) => mcp::run(&config, &pool, cmd).await,
        Command::Start { .. } => unreachable!("start serves"),
    }
}

/// The edge's own logs: errors only, unless `--debug`.
fn logging(debug: bool) {
    let level = if debug { tracing::Level::DEBUG } else { tracing::Level::ERROR };
    use tracing_subscriber::layer::SubscriberExt;
    use tracing_subscriber::util::SubscriberInitExt;
    tracing_subscriber::registry()
        .with(tracing_subscriber::fmt::layer().with_target(false).with_writer(std::io::stderr))
        .with(
            tracing_subscriber::filter::Targets::new()
                .with_target("jarvis_edge", level)
                .with_default(tracing::Level::WARN.min(level)),
        )
        .init();
}

/// The configuration and the database `main.py`'s `_run_db` would open,
/// created or migrated first, as its `init_db` does.
async fn open() -> Result<(crate::config::Config, SqlitePool), Fail> {
    let config = crate::config::Config::from_env().map_err(Fail::Error)?;
    let database = |e: String| Fail::Error(format!("database {}: {e}", config.db_path.display()));
    let pool = crate::db::pool(&config.db_path).map_err(|e| database(e.to_string()))?;
    crate::schema::init(&pool, &config.db_path).await.map_err(database)?;
    Ok((config, pool))
}

// ── Output ──────────────────────────────────────────────────────────────────

fn paint(code: &str, text: &str, tty: bool) -> String {
    if tty { format!("\x1b[{code}m{text}\x1b[0m") } else { text.to_string() }
}

/// Colour on a terminal, unless `NO_COLOR` says otherwise.
fn styled(code: &str, text: &str) -> String {
    let color = std::io::stdout().is_terminal() && std::env::var_os("NO_COLOR").is_none_or(|v| v.is_empty());
    paint(code, text, color)
}

pub fn green(text: &str) -> String {
    styled("32", text)
}

pub fn yellow(text: &str) -> String {
    styled("33", text)
}

pub fn red(text: &str) -> String {
    styled("31", text)
}

pub fn dim(text: &str) -> String {
    styled("2", text)
}

pub fn bold(text: &str) -> String {
    styled("1", text)
}

pub fn cyan(text: &str) -> String {
    styled("36", text)
}

/// `✓ {text}`.
pub fn ok(text: &str) -> String {
    format!("{} {text}", green("✓"))
}

/// A titled table of aligned columns.
pub fn table(title: &str, headers: &[&str], rows: &[Vec<String>]) {
    let mut widths: Vec<usize> = headers.iter().map(|h| crate::pystr::len(h)).collect();
    for row in rows {
        for (w, cell) in widths.iter_mut().zip(row) {
            *w = (*w).max(crate::pystr::len(cell));
        }
    }
    let line = |cells: Vec<String>| {
        let padded: Vec<String> = cells
            .into_iter()
            .zip(&widths)
            .map(|(c, w)| format!("{c}{}", " ".repeat(w.saturating_sub(crate::pystr::len(&c)))))
            .collect();
        padded.join("  ").trim_end().to_string()
    };
    println!("{}", bold(title));
    println!("{}", bold(&line(headers.iter().map(|h| h.to_string()).collect())));
    println!("{}", line(widths.iter().map(|w| "─".repeat(*w)).collect()));
    for row in rows {
        println!("{}", line(row.clone()));
    }
}

/// `f"{n:,}"`.
pub fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn groups_thousands_as_python_does() {
        assert_eq!(thousands(0), "0");
        assert_eq!(thousands(999), "999");
        assert_eq!(thousands(1000), "1,000");
        assert_eq!(thousands(1048576), "1,048,576");
        assert_eq!(thousands(-12345), "-12,345");
    }
}
