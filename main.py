import datetime
import logging
import pathlib
import threading
from pathlib import Path
from typing import Annotated, Optional
from uuid import uuid4

import typer
from rich import print as rprint
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn, TransferSpeedColumn
from rich.table import Table

from core.model_catalog import DEFAULT_MODEL

app = typer.Typer(help="General-purpose research and analysis agent.")
config_app = typer.Typer(help="Manage persistent configuration.")
app.add_typer(config_app, name="config")
model_app = typer.Typer(help="Manage models.")
app.add_typer(model_app, name="model")
memory_app = typer.Typer(help="Manage agent memory (AGENTS.md in the LangGraph store).")
app.add_typer(memory_app, name="memory")
maintenance_app = typer.Typer(help="Database maintenance tasks.")
app.add_typer(maintenance_app, name="maintenance")


@app.callback()
def _global_options(
    work_dir: Optional[Path] = typer.Option(
        None,
        "--work-dir",
        envvar="WORK_DIR",
        help="Working directory for databases and memory files. Defaults to cwd.",
    ),
) -> None:
    from core.config import configure
    configure(work_dir=work_dir)
console = Console()

REPORTS_DIR = pathlib.Path("reports")


def _setup_logging(debug: bool, work_dir: Path) -> None:
    from core.log_setup import setup_logging
    setup_logging(work_dir, level=logging.DEBUG if debug else logging.INFO, console=debug)


def _resolve_report_path(filename: str) -> pathlib.Path:
    """Resolve a report filename to a Path, handling missing extension or directory prefix."""
    p = pathlib.Path(filename)
    if p.exists():
        return p
    if not p.suffix:
        p = p.with_suffix(".md")
    if not p.exists():
        p = REPORTS_DIR / p.name
    return p


@app.command()
def run(
    query: Annotated[str, typer.Argument(help="The research question or topic to investigate.")],
    model: Annotated[str, typer.Option(help="Model identifier to use.")] = DEFAULT_MODEL,
    debug: Annotated[bool, typer.Option("--debug", help="Log to console at DEBUG level (default: write to ~/.jarvis/jarvis.log).")] = False,
    no_save: Annotated[bool, typer.Option("--no-save", help="Do not save the report to disk.")] = False,
):
    """Run the research agent on a query and display the report."""
    from core.agents import build_agent
    from core.config import get_config
    from db.ops import get_default_model
    # Always hits the DB (also hydrates the custom-model cache via _run_db) so a
    # custom --model resolves through get_model_spec before build_agent.
    default_model = _run_db(lambda s: get_default_model(s))
    if model == DEFAULT_MODEL:
        model = default_model
    _setup_logging(debug, get_config().work_dir)

    full_query = query
    if no_save:
        full_query += " Do not save the report to disk."

    from langgraph.checkpoint.memory import MemorySaver

    agent = build_agent(model, checkpointer=MemorySaver())
    result: dict = {}
    error: list[Exception] = []
    cli_thread_id = f"cli-{uuid4()}"

    from core.log_callback import AgentLogger

    async def _run() -> None:
        try:
            result.update(await agent.ainvoke(
                {"messages": [{"role": "user", "content": full_query}]},
                config={
                    "configurable": {"thread_id": cli_thread_id},
                    "recursion_limit": 100,
                    "callbacks": [AgentLogger()],
                },
            ))
        except Exception as exc:
            error.append(exc)

    with Progress(SpinnerColumn(), TextColumn("[bold blue]{task.description}"), console=console) as progress:
        progress.add_task("Running agents...", start=True, total=None)
        import asyncio
        asyncio.run(_run())

    if error:
        rprint(f"[bold red]Error:[/bold red] {error[0]}")
        raise typer.Exit(code=1)

    last_msg = result["messages"][-1]
    raw = getattr(last_msg, "content", "")
    if isinstance(raw, list):
        raw = " ".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text")
    output = raw or str(last_msg)
    console.print(Panel(Markdown(output), title="[bold green]Result[/bold green]", border_style="green"))


@app.command()
def reports():
    """List all saved reports."""
    if not REPORTS_DIR.exists() or not any(REPORTS_DIR.glob("*.md")):
        rprint("[yellow]No reports found.[/yellow]")
        raise typer.Exit()

    table = Table(title="Saved Reports", show_header=True, header_style="bold cyan")
    table.add_column("Filename", style="white")
    table.add_column("Size", justify="right", style="green")
    table.add_column("Modified", style="dim")

    for p in sorted(REPORTS_DIR.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True):
        stat = p.stat()
        size = f"{stat.st_size:,} B"
        modified = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        table.add_row(p.name, size, modified)

    console.print(table)


@app.command()
def view(
    filename: Annotated[str, typer.Argument(help="Report filename to view (name, name.md, or reports/name.md).")],
):
    """View a saved report rendered as Markdown."""
    path = _resolve_report_path(filename)
    if not path.exists():
        rprint(f"[bold red]Not found:[/bold red] {filename}")
        raise typer.Exit(code=1)

    content = path.read_text(encoding="utf-8")
    console.print(Panel(Markdown(content), title=f"[bold cyan]{path.name}[/bold cyan]", border_style="cyan"))


@app.command()
def start(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, help="TCP port."),
    debug: Annotated[bool, typer.Option("--debug", help="Log to console at DEBUG level (default: write to ~/.jarvis/jarvis.log).")] = False,
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev only)."),
):
    """Start the web server and stream agent results via SSE."""
    import uvicorn
    from core.config import get_config
    from server.entrypoint import app

    _setup_logging(debug, get_config().work_dir)

    url = f"http://{host}:{port}"
    console.print(
        Panel(
            f"[bold green]Server:[/bold green]  {url}\n"
            f"[bold cyan]Health:[/bold cyan]  {url}/health\n\n"
            f"[bold yellow]Example:[/bold yellow]\n"
            f"  curl -N -X POST {url}/run \\\n"
            f'    -H "Content-Type: application/json" \\\n'
            f"    -d '{{\"query\": \"Research the AI chip market\"}}'",
            title="[bold]Research Agent API[/bold]",
            border_style="green",
        )
    )

    uvicorn.run(app, host=host, port=port, reload=reload, log_config=None)


@app.command("download-voice")
def download_voice():
    """Download the Piper TTS voice model into the voices/ directory."""
    import httpx
    from core.config import get_config

    cfg = get_config()
    voice_path = Path(cfg.piper_voice)

    # Resolve relative paths against work_dir
    if not voice_path.is_absolute():
        voice_path = cfg.work_dir / voice_path

    voice_path.parent.mkdir(parents=True, exist_ok=True)

    # Parse voice name into HuggingFace URL components.
    # Format: {lang_region}-{speaker}-{quality}.onnx
    # e.g. en_US-hfc_female-medium → lang=en, region=en_US, speaker=hfc_female, quality=medium
    stem = voice_path.stem
    parts = stem.split("-")
    if len(parts) < 3:
        rprint(f"[bold red]Cannot parse voice name:[/bold red] {stem!r} (expected lang_region-speaker-quality)")
        raise typer.Exit(code=1)
    lang_region, speaker, quality = parts[0], parts[1], parts[2]
    lang = lang_region.split("_")[0]

    base = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
    prefix = f"{base}/{lang}/{lang_region}/{speaker}/{quality}"

    files = [
        (voice_path, f"{prefix}/{voice_path.name}"),
        (voice_path.parent / f"{voice_path.name}.json", f"{prefix}/{voice_path.name}.json"),
    ]

    console.print(f"[dim]Voice directory:[/dim] {voice_path.parent}")

    for dest, url in files:
        if dest.exists():
            console.print(f"[green]✓[/green] Already exists: {dest.name}")
            continue

        console.print(f"[blue]↓[/blue] Downloading {dest.name} ...")
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0)) or None
                with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task(dest.name, total=total)
                    with open(dest, "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=65536):
                            f.write(chunk)
                            progress.update(task, advance=len(chunk))
        except httpx.HTTPStatusError as exc:
            rprint(f"[bold red]HTTP {exc.response.status_code}:[/bold red] {url}")
            raise typer.Exit(code=1)

        console.print(f"[green]✓[/green] Saved: {dest}")

    console.print("[bold green]Done.[/bold green]")


def _run_db(coro):
    import asyncio
    from db.engine import init_db
    from db import async_session
    async def _inner():
        await init_db()
        async with async_session() as session:
            # Hydrate the runtime-added model cache so custom models resolve
            # in CLI paths (build_agent / model list / validation).
            from core.model_catalog import load_custom_models
            from db.ops import get_custom_models
            load_custom_models(await get_custom_models(session))
            return await coro(session)
    return asyncio.run(_inner())


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Config key (e.g. telegram.allowed_users)")],
    value: Annotated[str, typer.Argument(help="Value to store")],
) -> None:
    """Set a config value."""
    from db.ops import set_setting
    _run_db(lambda s: set_setting(s, key, value))
    rprint(f"[green]✓[/green] {key} = {value}")


@config_app.command("get")
def config_get(
    key: Annotated[str, typer.Argument(help="Config key to retrieve")],
) -> None:
    """Get a config value."""
    from db.ops import get_setting
    value = _run_db(lambda s: get_setting(s, key))
    if value is None:
        rprint(f"[yellow]Not set:[/yellow] {key}")
    else:
        rprint(f"{key} = {value}")


@config_app.command("list")
def config_list() -> None:
    """List all config settings."""
    from db.ops import list_settings
    rows = _run_db(lambda s: list_settings(s))
    if not rows:
        rprint("[yellow]No config settings found.[/yellow]")
        return
    table = Table(title="Config Settings", show_header=True, header_style="bold cyan")
    table.add_column("Key", style="white")
    table.add_column("Value", style="green")
    table.add_column("Updated", style="dim")
    for row in rows:
        table.add_row(row.key, row.value, row.updated_at.strftime("%Y-%m-%d %H:%M"))
    console.print(table)


@config_app.command("delete")
def config_delete(
    key: Annotated[str, typer.Argument(help="Config key to delete")],
) -> None:
    """Delete a config setting."""
    from db.ops import delete_setting
    deleted = _run_db(lambda s: delete_setting(s, key))
    if deleted:
        rprint(f"[green]✓[/green] Deleted: {key}")
    else:
        rprint(f"[yellow]Not found:[/yellow] {key}")


@model_app.command("list")
def model_list() -> None:
    """List all available models (built-in + custom)."""
    from core.model_catalog import BUILTIN_MODELS, available_models
    from db.ops import get_default_model
    # _run_db hydrates the custom-model cache before this read.
    current_default = _run_db(lambda s: get_default_model(s))
    builtin_ids = {m.id for m in BUILTIN_MODELS}

    table = Table(title="Available Models", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="white")
    table.add_column("Label", style="green")
    table.add_column("Source", style="magenta")
    table.add_column("", style="yellow")
    for m in available_models():
        marker = "◀ default" if m.id == current_default else ""
        source = "built-in" if m.id in builtin_ids else "custom"
        table.add_row(m.id, m.label, source, marker)
    console.print(table)


@model_app.command("add")
def model_add(
    model_id: Annotated[str, typer.Argument(help="Model ID, e.g. google_genai:gemini-3.5-flash")],
    label: Annotated[str, typer.Argument(help="Display label shown in selectors, e.g. 'Gemini 3.5 Flash'")],
    provider: Annotated[
        str | None,
        typer.Option(help="Provider; inferred from the ID prefix (text before ':') when omitted."),
    ] = None,
    context_window: Annotated[
        int | None,
        typer.Option(help="Input token limit, when you know it — sizes this model's compaction "
                          "threshold. Omit rather than guess; unknown falls back to a flat 80k."),
    ] = None,
) -> None:
    """Add a model to the catalog at runtime — no code change needed.

    The ID must be 'provider:model_name' where provider is one of the supported
    backends. The model_name is passed verbatim to the provider's SDK.
    """
    from core.model_catalog import KNOWN_PROVIDERS, provider_from_id
    from db.ops import add_custom_model
    prov = provider or provider_from_id(model_id)
    if not prov or ":" not in model_id:
        rprint(f"[red]Invalid model ID:[/red] {model_id}\nExpected 'provider:model_name', e.g. google_genai:gemini-3.5-flash")
        raise typer.Exit(code=1)
    if prov not in KNOWN_PROVIDERS:
        rprint(
            f"[red]Unsupported provider:[/red] {prov}\n"
            f"Must be one of: {', '.join(sorted(KNOWN_PROVIDERS))}"
        )
        raise typer.Exit(code=1)
    _run_db(lambda s: add_custom_model(s, model_id, label, prov, context_window))
    win = f" [{context_window:,} ctx]" if context_window else ""
    rprint(f"[green]✓[/green] Added model: {model_id} ({label}) [{prov}]{win}")
    rprint("[dim]Web UI picks it up on the next 'models' query; a running server validates it after that.[/dim]")


@model_app.command("remove")
def model_remove(
    model_id: Annotated[str, typer.Argument(help="Custom model ID to remove")],
) -> None:
    """Remove a custom model. Built-in models cannot be removed."""
    from db.ops import remove_custom_model
    removed = _run_db(lambda s: remove_custom_model(s, model_id))
    if removed:
        rprint(f"[green]✓[/green] Removed model: {model_id}")
    else:
        rprint(f"[yellow]Not a custom model:[/yellow] {model_id} (built-ins can't be removed)")


@model_app.command("sync")
def model_sync(
    provider: Annotated[
        str | None,
        typer.Argument(help="Provider to sync; omit to sync every discoverable provider."),
    ] = None,
    probe: Annotated[
        bool,
        typer.Option("--probe", help="Also issue a real one-token call per catalog model. "
                                     "Listing is not entitlement — this is what catches a "
                                     "model that is published but 404s for your account."),
    ] = False,
    add_new: Annotated[
        bool,
        typer.Option("--add-new", help="Register newly discovered models into the custom-model "
                                       "layer (same store as 'model add')."),
    ] = False,
    include_non_chat: Annotated[
        bool,
        typer.Option("--include-non-chat", help="Include models whose names suggest they generate "
                                                "speech/images/music. Shown either way; this only "
                                                "affects what --add-new registers."),
    ] = False,
) -> None:
    """Diff the catalog against what each provider actually offers.

    Read-only by default: discovery reports drift and supplies metadata, but what
    the catalog *says* stays with BUILTIN_MODELS + the custom layer. See
    core/model_discovery.py for why it can't simply be the source of truth.
    """
    from core.model_catalog import KNOWN_PROVIDERS
    from core.model_discovery import (
        DISCOVERABLE, DiscoveryError, build_report, discover, probe as probe_model,
    )
    from db.ops import add_custom_model, get_default_model

    if provider is not None and provider not in KNOWN_PROVIDERS:
        rprint(f"[red]Unknown provider:[/red] {provider}\n"
               f"Must be one of: {', '.join(sorted(KNOWN_PROVIDERS))}")
        raise typer.Exit(code=1)
    if provider is not None and provider not in DISCOVERABLE:
        rprint(f"[yellow]No discovery adapter for '{provider}'.[/yellow] "
               f"Discoverable: {', '.join(sorted(DISCOVERABLE))}")
        raise typer.Exit(code=1)

    targets = [provider] if provider else sorted(DISCOVERABLE)
    # Hydrates the custom-model cache so available_models() sees runtime additions.
    _run_db(lambda s: get_default_model(s))

    any_drift = False
    for prov in targets:
        try:
            found = discover(prov)
        except DiscoveryError as exc:
            rprint(f"\n[bold]{prov}[/bold]  [yellow]skipped:[/yellow] {exc}")
            continue

        report = build_report(prov, found)
        if probe:
            from core.model_catalog import available_models
            for spec in [m for m in available_models() if m.provider == prov]:
                ok, why = probe_model(spec.id)
                if not ok:
                    report.unreachable.append((spec.id, why))

        rprint(f"\n[bold]{prov}[/bold]  [dim]{len(found)} model(s) offered[/dim]")
        if report.clean:
            rprint("  [green]✓[/green] catalog matches the provider")
            continue
        any_drift = True

        if report.missing:
            rprint("  [red]gone[/red] — in the catalog, no longer offered:")
            for mid in report.missing:
                rprint(f"      {mid}")
        if report.unreachable:
            rprint("  [red]unreachable[/red] — offered but this credential cannot call it:")
            for mid, why in report.unreachable:
                rprint(f"      {mid}\n        [dim]{why}[/dim]")
        if report.window_backfill:
            rprint("  [cyan]context_window available[/cyan] — catalog has None:")
            for mid, win in report.window_backfill:
                rprint(f"      {mid}  →  {win:,}")
        if report.window_drift:
            rprint("  [yellow]context_window differs[/yellow]:")
            for mid, ours, theirs in report.window_drift:
                rprint(f"      {mid}  catalog={ours:,}  provider={theirs:,}")
        if report.new:
            chat_new = [m for m in report.new if m.likely_chat]
            other_new = [m for m in report.new if not m.likely_chat]
            rprint(f"  [green]new[/green] — offered, not in the catalog ({len(chat_new)}):")
            for m in chat_new:
                win = f"  [{m.context_window:,} ctx]" if m.context_window else ""
                rprint(f"      {m.id}  [dim]{m.label}[/dim]{win}")
            if other_new:
                rprint(f"  [dim]non-chat (name suggests speech/image/music), "
                       f"not added unless --include-non-chat ({len(other_new)}):[/dim]")
                for m in other_new:
                    rprint(f"      [dim]{m.id}  {m.label}[/dim]")
            if add_new:
                to_add = report.new if include_non_chat else chat_new
                for m in to_add:
                    _run_db(lambda s, m=m: add_custom_model(
                        s, m.id, m.label, m.provider, m.context_window,
                    ))
                rprint(f"  [green]✓[/green] added {len(to_add)} model(s) to the custom layer")

    if any_drift and not add_new:
        rprint("\n[dim]Read-only. Re-run with --add-new to register the new models; "
               "'gone' entries in BUILTIN_MODELS need a code change.[/dim]")


@model_app.command("set-default")
def model_set_default(
    model_id: Annotated[str, typer.Argument(help="Model ID (copy from 'model list')")],
) -> None:
    """Set the default model used when no model is specified."""
    from core.model_catalog import is_valid_model
    from db.ops import set_setting
    async def _work(s) -> bool:
        # _run_db hydrates the custom-model cache before this runs, so
        # is_valid_model also accepts runtime-added models.
        if not is_valid_model(model_id):
            return False
        await set_setting(s, "default.model", model_id)
        return True
    if not _run_db(_work):
        rprint(f"[red]Unknown model:[/red] {model_id}\nRun 'model list' to see available IDs.")
        raise typer.Exit(code=1)
    rprint(f"[green]✓[/green] Default model set to: {model_id}")


# ── memory subcommands ──────────────────────────────────────────────────────
#
# AGENTS.md is stored in the LangGraph AsyncSqliteStore, which lives in the
# checkpoints DB (NOT database.db). Path defaults to ~/.jarvis/checkpoints.db
# but is overridable via CHECKPOINTS_DB or --work-dir. The store row uses
# prefix='memory', key='AGENTS.md', and a JSON value with a "content" field.

_MEMORY_PREFIX = "memory"
_MEMORY_KEY = "AGENTS.md"


def _memory_db_path() -> Path:
    from core.config import get_config
    return Path(get_config().checkpoints_db)


def _memory_connect():
    import sqlite3
    db_path = _memory_db_path()
    if not db_path.exists():
        rprint(f"[red]checkpoints DB not found:[/red] {db_path}\nStart the server once to initialize it.")
        raise typer.Exit(code=1)
    return sqlite3.connect(db_path)


@memory_app.command("show")
def memory_show() -> None:
    """Print the current AGENTS.md memory stored in the LangGraph store."""
    import json
    con = _memory_connect()
    try:
        row = con.execute(
            "SELECT value, updated_at FROM store WHERE prefix=? AND key=?",
            (_MEMORY_PREFIX, _MEMORY_KEY),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        rprint("[yellow]No memory entry stored.[/yellow] The agent will use only the hardcoded system prompt.")
        return
    value, updated_at = row
    try:
        content = json.loads(value).get("content", "")
    except json.JSONDecodeError:
        content = value
    rprint(f"[dim]Updated: {updated_at} ({len(content)} chars)[/dim]\n")
    console.print(Panel(Markdown(content), title="[bold cyan]AGENTS.md[/bold cyan]", border_style="cyan"))


@memory_app.command("reset")
def memory_reset(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete the AGENTS.md memory entry. The agent will fall back to the hardcoded system prompt."""
    db_path = _memory_db_path()
    if not yes:
        confirm = typer.confirm(f"Delete agent memory in {db_path}?", default=False)
        if not confirm:
            rprint("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=1)
    con = _memory_connect()
    try:
        cur = con.execute(
            "DELETE FROM store WHERE prefix=? AND key=?",
            (_MEMORY_PREFIX, _MEMORY_KEY),
        )
        con.commit()
        if cur.rowcount == 0:
            rprint("[yellow]No memory entry to delete.[/yellow]")
        else:
            rprint(f"[green]✓[/green] Deleted memory entry from {db_path}")
    finally:
        con.close()


@memory_app.command("set")
def memory_set(
    file: Annotated[Path, typer.Argument(help="Path to a markdown file whose contents replace the stored AGENTS.md.")],
) -> None:
    """Replace AGENTS.md memory with the contents of a local file."""
    import json
    from datetime import datetime, timezone

    if not file.exists():
        rprint(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)
    content = file.read_text(encoding="utf-8")
    if not content.strip():
        rprint("[red]Refusing to set an empty memory entry.[/red] Use 'memory reset' instead.")
        raise typer.Exit(code=1)

    value = json.dumps({"content": content})
    now = datetime.now(timezone.utc).isoformat()
    con = _memory_connect()
    try:
        con.execute(
            "INSERT INTO store (prefix, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(prefix, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (_MEMORY_PREFIX, _MEMORY_KEY, value, now, now),
        )
        con.commit()
    finally:
        con.close()
    rprint(f"[green]✓[/green] Wrote {len(content)} chars from {file} to memory in {_memory_db_path()}")


# ── maintenance subcommands ─────────────────────────────────────────────────
#
# LangGraph's SqliteSaver writes a full state snapshot at every graph
# super-step and never prunes, so checkpoints.db grows without bound (a long
# conversation can accumulate hundreds of snapshots, each carrying the entire
# message history). This app only ever reads the *latest* checkpoint per thread
# — resume, the todos query, and conversation continuity all call aget_tuple
# without a checkpoint_id, which returns the newest — and does no replay or
# time-travel, so older per-thread checkpoints are dead weight.

_KEEP_LATEST_PER_THREAD = """
    {verb} FROM {table}
    WHERE checkpoint_id <> (
      SELECT MAX(c2.checkpoint_id) FROM checkpoints c2
      WHERE c2.thread_id = {table}.thread_id
        AND c2.checkpoint_ns = {table}.checkpoint_ns
    )
"""


@maintenance_app.command("prune-checkpoints")
def prune_checkpoints(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report what would be removed without deleting anything.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Shrink checkpoints.db by keeping only the latest checkpoint per thread.

    Stop the server first so nothing else holds the DB open. Resumable state
    for every conversation is preserved; only superseded snapshots are dropped.
    """
    import sqlite3

    db_path = _memory_db_path()  # checkpoints.db (shared with the memory store)
    if not db_path.exists():
        rprint(f"[red]checkpoints DB not found:[/red] {db_path}")
        raise typer.Exit(code=1)

    size_before = db_path.stat().st_size
    con = sqlite3.connect(db_path)
    con.isolation_level = None  # explicit txn control; VACUUM can't run inside one
    try:
        total_cp = con.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
        threads = con.execute("SELECT count(DISTINCT thread_id) FROM checkpoints").fetchone()[0]
        keep_cp = con.execute(
            "SELECT count(*) FROM checkpoints WHERE checkpoint_id = ("
            " SELECT MAX(c2.checkpoint_id) FROM checkpoints c2"
            " WHERE c2.thread_id = checkpoints.thread_id"
            " AND c2.checkpoint_ns = checkpoints.checkpoint_ns)"
        ).fetchone()[0]
        prunable = total_cp - keep_cp

        rprint(f"[dim]DB:[/dim] {db_path}  [dim]({size_before / 1e6:.0f} MB)[/dim]")
        rprint(
            f"[dim]Threads:[/dim] {threads}   [dim]Checkpoints:[/dim] {total_cp} "
            f"[dim](keep {keep_cp}, prune[/dim] [bold]{prunable}[/bold][dim])[/dim]"
        )

        if prunable <= 0:
            rprint("[green]Nothing to prune.[/green]")
            return
        if dry_run:
            rprint(f"[yellow]Dry run:[/yellow] would delete {prunable} checkpoints + their writes, then VACUUM.")
            return
        if not yes and not typer.confirm(f"Delete {prunable} superseded checkpoints from {db_path}?", default=False):
            rprint("[yellow]Aborted.[/yellow]")
            raise typer.Exit(code=1)

        con.execute("BEGIN")
        con.execute(_KEEP_LATEST_PER_THREAD.format(verb="DELETE", table="writes"))
        con.execute(_KEEP_LATEST_PER_THREAD.format(verb="DELETE", table="checkpoints"))
        con.execute("COMMIT")
        con.execute("VACUUM")
    finally:
        con.close()

    size_after = db_path.stat().st_size
    saved = (1 - size_after / size_before) * 100 if size_before else 0
    rprint(
        f"[green]✓[/green] Pruned {prunable} checkpoints. "
        f"{size_before / 1e6:.0f} MB → {size_after / 1e6:.0f} MB "
        f"([bold]{saved:.0f}%[/bold] smaller)."
    )


if __name__ == "__main__":
    app()
