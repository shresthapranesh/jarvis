import datetime
import logging
import pathlib
import threading
from pathlib import Path
from typing import Annotated, Optional

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
    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(message)s",
            handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
        )
    else:
        from logging.handlers import RotatingFileHandler
        work_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            handlers=[RotatingFileHandler(work_dir / "jarvis.log", maxBytes=10 * 1024 * 1024, backupCount=3)],
        )


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
    if model == DEFAULT_MODEL:
        model = _run_db(lambda s: get_default_model(s))
    _setup_logging(debug, get_config().work_dir)

    full_query = query
    if no_save:
        full_query += " Do not save the report to disk."

    agent = build_agent(model)
    result: dict = {}
    error: list[Exception] = []

    def _run():
        try:
            result.update(agent.invoke({"messages": [{"role": "user", "content": full_query}]}))
        except Exception as exc:
            error.append(exc)

    with Progress(SpinnerColumn(), TextColumn("[bold blue]{task.description}"), console=console) as progress:
        progress.add_task("Running agents...", start=True, total=None)
        t = threading.Thread(target=_run)
        t.start()
        t.join()

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

    uvicorn.run(app, host=host, port=port, reload=reload)


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
    """List all available models."""
    from core.model_catalog import AVAILABLE_MODELS
    from db.ops import get_default_model
    current_default = _run_db(lambda s: get_default_model(s))

    table = Table(title="Available Models", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="white")
    table.add_column("Label", style="green")
    table.add_column("", style="yellow")
    for m in AVAILABLE_MODELS:
        marker = "◀ default" if m.id == current_default else ""
        table.add_row(m.id, m.label, marker)
    console.print(table)


@model_app.command("set-default")
def model_set_default(
    model_id: Annotated[str, typer.Argument(help="Model ID (copy from 'model list')")],
) -> None:
    """Set the default model used when no model is specified."""
    from core.model_catalog import is_valid_model
    from db.ops import set_setting
    if not is_valid_model(model_id):
        rprint(f"[red]Unknown model:[/red] {model_id}\nRun 'model list' to see available IDs.")
        raise typer.Exit(code=1)
    _run_db(lambda s: set_setting(s, "default.model", model_id))
    rprint(f"[green]✓[/green] Default model set to: {model_id}")


if __name__ == "__main__":
    app()
