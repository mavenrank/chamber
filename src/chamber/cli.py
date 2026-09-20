"""`chamber` — the command line.

    chamber doctor                        check the setup
    chamber run "find the cheapest ..."   drive the browser with a model
    chamber open https://example.com      open the window and observe, no model
    chamber mcp                           serve over MCP for another harness

Output goes through rich because the point of this project is watchability, and a
run that prints a wall of JSON is not watchable. The browser window is the primary
display; the terminal is the transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from chamber import paths
from chamber.config import ChamberConfig
from chamber.session import Chamber

_HELP = """\
A headed browser an LLM can drive in the open.

[bold]Start here[/bold]

  [cyan]chamber doctor[/cyan]                          check the browser, extension and models
  [cyan]chamber setup[/cyan]                           configure the browser yourself, agent off
  [cyan]chamber watch[/cyan] "find the cheapest X"     run a task with the full live view

[bold]Running tasks[/bold]

  chamber run "summarise this page" --url https://example.com
  chamber run "..." --model glm-5.2 --max-steps 60
  chamber watch "..."                     same, plus every model call on screen

[bold]Looking at what happened[/bold]

  chamber trace                           list past runs
  chamber trace <run-id>                  the full tree: steps, actions, sources
  chamber trace <run-id> --llm            full prompts, outputs and tool calls
  chamber see <url> --ask "is it broken?" screenshot and have the vision model look

[bold]Housekeeping[/bold]

  chamber open <url>                      a plain window; log in by hand
  chamber profiles                        list profiles and their sizes
  chamber mcp                             serve to Claude Code or another harness

[dim]Configuration lives in .env — see .env.example. Every command takes
--profile to pick which browser profile to use.[/dim]
"""

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=_HELP,
)

def _force_utf8_output() -> None:
    """Windows' legacy console is cp1252 and raises on anything outside it.

    Applied once, for every command. It used to live inside `watch` alone, which
    was not enough: rich falls back to its legacy Windows renderer whenever output
    is not a real terminal — piped to a file, or through `tail` — and then a single
    box-drawing character in a status line takes the whole run down with a
    `UnicodeEncodeError`. Losing a finished run to a glyph is not a trade worth
    making, so unencodable characters are replaced rather than raised.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


_force_utf8_output()
console = Console()


def run_async(coro):
    """Run a coroutine, and exit quietly on Ctrl+C.

    Interrupting a chamber command tears down a live browser, and Playwright's
    driver connection drops mid-teardown. That surfaces as a stack trace and a
    stray `Future exception was never retrieved: Connection closed while reading
    from the driver` *after* the command has already said it is closing — noise
    about an orderly shutdown, which reads like a crash.

    The loop's exception handler swallows exactly that case and nothing else.
    """
    async def _wrapped():
        loop = asyncio.get_running_loop()
        default = loop.get_exception_handler()

        def handler(lp, ctx):
            message = str(ctx.get("exception") or ctx.get("message") or "")
            if "Connection closed while reading from the driver" in message:
                return  # the browser going away during shutdown, as expected
            (default or lp.default_exception_handler)(ctx)

        loop.set_exception_handler(handler)
        return await coro

    try:
        return asyncio.run(_wrapped())
    except KeyboardInterrupt:
        console.print("[dim]interrupted[/dim]")
        raise typer.Exit(130) from None


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )
    # Playwright's own logging is extremely chatty at DEBUG and drowns ours.
    logging.getLogger("chamber").setLevel(logging.DEBUG if verbose else logging.INFO)


# --------------------------------------------------------------------- doctor


@app.command()
def doctor() -> None:
    """Check that everything chamber needs is present and working."""
    from chamber.browser.discovery import discover_browser, find_ubo

    console.print("\n[bold]chamber doctor[/bold]\n")
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("")
    table.add_column("check")
    table.add_column("detail", overflow="fold")

    ok = True

    def row(passed: bool | None, name: str, detail: str) -> None:
        nonlocal ok
        if passed is False:
            ok = False
        mark = {True: "[green]✓[/green]", False: "[red]✗[/red]", None: "[yellow]•[/yellow]"}[passed]
        table.add_row(mark, name, detail)

    # Browser
    try:
        build = discover_browser()
        row(True, "browser", f"{build.family} — {build.path}")
        if build.mv2:
            row(True, "manifest v2", "this build can run real uBlock Origin")
        else:
            row(False, "manifest v2", build.note or "this build cannot run MV2 extensions")
    except FileNotFoundError as exc:
        row(False, "browser", str(exc).split("\n")[0])
        build = None

    # Extension
    ubo = find_ubo()
    if ubo:
        import json

        manifest = json.loads((ubo / "manifest.json").read_text(encoding="utf-8"))
        mv = manifest.get("manifest_version")
        row(
            mv == 2,
            "uBlock Origin",
            f"{manifest.get('version')} (manifest v{mv}) — {ubo}",
        )
    else:
        row(False, "uBlock Origin", "not installed — run: uv run python scripts/install_ubo.py")

    # Model
    cfg = ChamberConfig.from_env()
    if cfg.model.api_key:
        row(True, "model", f"{cfg.model.provider}/{cfg.model.model} @ {cfg.model.base_url or 'default'}")
    else:
        row(
            None,
            "model",
            f"{cfg.model.provider}/{cfg.model.model} — no API key set. "
            "Copy .env.example to .env. (Not needed for `chamber open` or MCP.)",
        )

    # Planner — optional; without it the step model plans for itself.
    if cfg.orchestrator:
        row(
            True,
            "planner",
            f"{cfg.orchestrator.model} @ {cfg.orchestrator.base_url} "
            "(called on a plan change, not every step)",
        )
    else:
        row(
            None,
            "planner",
            "not configured — the step model plans for itself. Set "
            "CHAMBER_ORCHESTRATOR_MODEL to split planning from acting.",
        )

    # Vision — optional, so its absence is a note rather than a failure.
    if cfg.vision:
        row(True, "vision", f"{cfg.vision.model} @ {cfg.vision.base_url}")
        residency = _ollama_residency(cfg.vision)
        if residency:
            loaded, detail = residency
            row(loaded, "vision GPU", detail)
    else:
        row(
            None,
            "vision",
            "not configured — screenshots save to disk but nothing looks at them. "
            "Set CHAMBER_VISION_MODEL (e.g. qwen2.5vl:3b via Ollama).",
        )

    # Paths
    row(True, "state", str(paths.home()))
    profiles = sorted(p.name for p in (paths.home() / "profiles").glob("*") if p.is_dir())
    row(None, "profiles", ", ".join(profiles) if profiles else "(none yet)")

    console.print(table)
    console.print()

    if build is not None:
        console.print("[dim]launching the browser to verify the extension actually loads…[/dim]")
        loaded = run_async(_verify_extension())
        if loaded is None:
            console.print("[red]✗[/red] the browser failed to launch")
            ok = False
        elif not loaded:
            console.print("[yellow]•[/yellow] no extensions reported as loaded")
        else:
            for ext in loaded:
                state = "enabled" if ext.get("enabled") else "[red]disabled[/red]"
                console.print(
                    f"  [green]✓[/green] {ext.get('name')} {ext.get('version') or ''} — {state}"
                )
                if not ext.get("enabled"):
                    ok = False

    console.print()
    console.print("[green]all good[/green]" if ok else "[red]problems found — see above[/red]")
    raise typer.Exit(0 if ok else 1)


def _nvidia_gpu() -> str | None:
    """Which NVIDIA GPU is running inference, if any.

    Ollama's `/api/ps` reports `size_vram`, which distinguishes GPU memory from
    system RAM — but *not which GPU*. On a laptop with both a discrete NVIDIA card
    and an Intel iGPU that is exactly the ambiguity that matters, so this asks
    nvidia-smi whether the inference process is actually on the NVIDIA device.
    """
    import shutil
    import subprocess

    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        apps = subprocess.run(
            [smi, "--query-compute-apps=process_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
        gpus = subprocess.run(
            [smi, "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None

    if "ollama" not in apps.lower() and "llama-server" not in apps.lower():
        return None
    return gpus.splitlines()[0].strip() if gpus else None


def _ollama_residency(vision) -> tuple[bool, str] | None:
    """Is the vision model actually on the GPU, or quietly on the CPU?

    Worth checking rather than assuming: Ollama falls back to CPU silently when a
    model does not fit, and the only symptom is that everything is slow. It also
    evicts after an idle period, and a cold reload costs ~90s — so a model that
    was on the GPU a minute ago may not be now.
    """
    import json
    import urllib.error
    import urllib.request

    base = (vision.base_url or "").rstrip("/")
    if "11434" not in base:
        return None
    root = base[: -len("/v1")] if base.endswith("/v1") else base

    try:
        with urllib.request.urlopen(f"{root}/api/ps", timeout=5) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"Ollama not reachable at {root} ({type(exc).__name__})"

    for entry in data.get("models", []):
        if entry.get("name", "").startswith(vision.model.split(":")[0]):
            total = entry.get("size", 0) or 1
            vram = entry.get("size_vram", 0)
            pct = vram * 100 // total
            if pct >= 95:
                # Name the actual device — "in VRAM" alone does not say which GPU
                # on a laptop that has two.
                device = _nvidia_gpu()
                where = f" on {device}" if device else " (device unidentified)"
                return True, f"{vram / 1e9:.2f} GB, {pct}% resident in VRAM{where}"
            return False, (
                f"only {pct}% in VRAM ({(total - vram) / 1e9:.2f} GB on CPU) — "
                "it will be very slow. Close other GPU applications."
            )

    return True, (
        "not loaded right now (Ollama unloads when idle). Chamber sends "
        f"keep_alive={vision.keep_alive} so it stays resident during a run."
    )


async def _verify_extension() -> list[dict] | None:
    cfg = ChamberConfig.from_env(profile="doctor")
    try:
        async with Chamber.open(cfg) as ch:
            return await ch.extension_status()
    except Exception as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        return None


# ------------------------------------------------------------------------ run


@app.command()
def run(
    task: Annotated[
        str,
        typer.Argument(
            help='What you want done, in plain language. Quote it: chamber run "find X"'
        ),
    ],
    url: Annotated[str, typer.Option("--url", "-u", help="Start here.")] = "",
    profile: Annotated[str, typer.Option("--profile", "-p", help="Which browser profile.")] = "default",
    model: Annotated[str, typer.Option("--model", "-m", help="Override CHAMBER_MODEL.")] = "",
    max_steps: Annotated[int, typer.Option("--max-steps", help="Hard stop.")] = 40,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Give the agent a task and watch it work.

    The browser window is the main display; this terminal is the transcript. Use
    'chamber watch' instead to also see every model call and reply.

    Examples:

      chamber run "what changed in Chrome 151?" --url https://duckduckgo.com

      chamber run "add a wireless mouse to the basket" --profile shopping

      chamber run "find the cheapest X" --model glm-5.2 --max-steps 60
    """
    _setup_logging(verbose)
    overrides: dict[str, object] = {"profile": profile, "loop__max_steps": max_steps}
    if model:
        overrides["model__model"] = model
    cfg = ChamberConfig.from_env(**overrides)

    if not cfg.model.api_key:
        console.print(
            "[red]No API key.[/red] Copy [bold].env.example[/bold] to [bold].env[/bold] "
            "and set one, or export CHAMBER_OPENAI_API_KEY."
        )
        raise typer.Exit(1)

    result = run_async(_run_task(cfg, task, url or None))

    console.print()
    console.print(
        Panel(
            result.summary,
            title="[bold]done[/bold]" if result.success else "[bold yellow]incomplete[/bold yellow]",
            border_style="green" if result.success else "yellow",
        )
    )
    console.print(
        f"[dim]{result.step_count} steps · {result.elapsed_s:.0f}s · "
        f"{result.input_tokens:,} in / {result.output_tokens:,} out tokens · "
        f"stopped because {result.stopped_because}[/dim]"
    )
    raise typer.Exit(0 if result.success else 1)


async def _run_task(cfg: ChamberConfig, task: str, url: str | None):
    from chamber.agent.loop import run_task

    def on_event(event: str, payload: dict) -> None:
        if event == "run_start":
            console.print(Panel(payload["task"], title="task", border_style="cyan"))
        elif event == "observe":
            console.print(
                f"\n[bold cyan]step {payload['step']}[/bold cyan] "
                f"[dim]{payload['controls']} controls · {payload['url'][:70]}[/dim]"
            )
        elif event == "thought" and payload.get("text"):
            console.print(f"  [italic]{payload['text']}[/italic]")
            if payload.get("repairs"):
                console.print(f"  [dim yellow]format fixed: {'; '.join(payload['repairs'])}[/dim yellow]")
        elif event == "action":
            mark = "[green]✓[/green]" if payload["ok"] else "[red]✗[/red]"
            console.print(f"  {mark} [bold]{payload['name']}[/bold] [dim]{payload['detail']}[/dim]")
        elif event == "stuck":
            tail = " — asking the vision model" if payload.get("vision") else " — nudging it to read more"
            console.print(
                f"  [yellow]⚠ nothing changed for {payload['count']} steps{tail}[/yellow]"
            )
        elif event == "plan":
            # The planner runs a handful of times per run, so printing the whole
            # plan each time is cheap and is the only window onto what it decided.
            console.print()
            console.print(f"[bold blue]▣ planner[/bold blue] [dim]{payload.get('assessment', '')}[/dim]")
            for i, stage in enumerate(payload.get("stages", [])):
                if i < payload.get("current", 0):
                    console.print(f"    [dim green]✓ {stage}[/dim green]")
                elif i == payload.get("current", 0):
                    console.print(f"    [bold]➤ {stage}[/bold]")
                else:
                    console.print(f"    [dim]· {stage}[/dim]")
        elif event == "vision":
            console.print(f"  [magenta]👁 {payload['text']}[/magenta]")
        elif event == "run_end" and payload.get("via_tools", 0) + payload.get("via_text", 0):
            rate = payload["tool_rate"]
            style = "green" if rate > 0.9 else "yellow"
            console.print(
                f"[dim]decisions: {payload['via_tools']} via tool call, "
                f"{payload['via_text']} via text — [/dim][{style}]{rate:.0%} tool rate[/{style}]"
            )
        elif event == "vision_ready":
            if payload["loaded"]:
                console.print(f"[dim]vision model resident ({payload['seconds']:.1f}s)[/dim]")
            else:
                console.print("[yellow]vision model did not preload; it will load on first use[/yellow]")
        elif event == "parse_retry":
            console.print(f"  [yellow]↻ reply unreadable, retrying ({payload['attempt']})[/yellow]")

    async with Chamber.open(cfg) as ch:
        console.print(f"[dim]{ch.build.label} · profile {cfg.profile}[/dim]")
        return await run_task(ch, task, start_url=url, on_event=on_event)


# ----------------------------------------------------------------------- open


# Named explicitly because the function cannot be called `open` (that shadows the
# builtin) and typer would otherwise derive the command name `open-` from the
# trailing underscore. Registering it twice — once bare, once by name — is what put
# both `open` and `open-` in the command list.
@app.command(name="open")
def open_(
    url: Annotated[str, typer.Argument(help="Page to open.")] = "about:blank",
    profile: Annotated[str, typer.Option("--profile", "-p")] = "default",
    show: Annotated[bool, typer.Option("--show", help="Print what a model would see.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Open the browser with no model attached.

    Use this to log into a site by hand — the profile keeps the session and later
    agent runs inherit it. To configure the browser itself, prefer 'chamber setup',
    which also switches the agent off.
    """
    _setup_logging(verbose)
    cfg = ChamberConfig.from_env(profile=profile)
    run_async(_open(cfg, url, show))


async def _open(cfg: ChamberConfig, url: str, show: bool) -> None:
    async with Chamber.open(cfg) as ch:
        console.print(f"[dim]{ch.build.label} · profile {cfg.profile}[/dim]")
        if url and url != "about:blank":
            await ch.goto(url)
        await ch.overlay.think(
            goal="manual session",
            thought="No model attached. Log in or browse; I'll keep the profile.",
            status="idle",
        )
        if show:
            snap = await ch.observe()
            console.print(Panel(ch.render(), title=snap.title or "page"))

        console.print("\n[bold]browser is open.[/bold] press Ctrl+C to close it.\n")
        try:
            while True:
                await asyncio.sleep(1)
                if not ch.tab_ids():
                    break
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("closing…")


# ------------------------------------------------------------------------ mcp


@app.command()
def mcp(
    profile: Annotated[str, typer.Option("--profile", "-p")] = "default",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Serve the browser over MCP so another harness can drive it."""
    _setup_logging(verbose)
    try:
        from chamber.mcp.server import serve
    except ImportError:
        console.print(
            "[red]The MCP extra is not installed.[/red] Run: [bold]uv sync --extra mcp[/bold]"
        )
        raise typer.Exit(1) from None

    cfg = ChamberConfig.from_env(profile=profile)
    asyncio.run(serve(cfg))


# --------------------------------------------------------------------- setup


@app.command()
def watch(
    task: Annotated[str, typer.Argument(help="What you want done, in plain language.")],
    url: Annotated[str, typer.Option("--url", "-u", help="Start here.")] = "",
    profile: Annotated[str, typer.Option("--profile", "-p")] = "default",
    model: Annotated[str, typer.Option("--model", "-m")] = "",
    max_steps: Annotated[int, typer.Option("--max-steps", help="Hard stop.")] = 40,
) -> None:
    """Run a task with a live view of every model call and every action.

    Example:

      chamber watch "compare laptop prices on amazon.in and amazon.com"


    Same run as `chamber run`, with the whole data flow on screen: what the planner
    decided, what the step model was asked and answered, whether it called a tool or
    wrote prose, what the vision model saw, and every action and result.
    """
    # The view owns the terminal, so ordinary logging would tear the layout apart.
    logging.basicConfig(level=logging.CRITICAL, handlers=[logging.NullHandler()])
    overrides: dict[str, object] = {"profile": profile, "loop__max_steps": max_steps}
    if model:
        overrides["model__model"] = model
    cfg = ChamberConfig.from_env(**overrides)
    if not cfg.model.api_key:
        console.print("[red]No API key.[/red] Copy .env.example to .env and set one.")
        raise typer.Exit(1)

    result = run_async(_watch(cfg, task, url or None, max_steps))
    console.print()
    console.print(
        Panel(
            result.summary,
            title="[bold]done[/bold]" if result.success else "[bold yellow]incomplete[/bold yellow]",
            border_style="green" if result.success else "yellow",
        )
    )
    raise typer.Exit(0 if result.success else 1)


async def _watch(cfg: ChamberConfig, task: str, url: str | None, max_steps: int):
    from chamber.agent.loop import run_task
    from chamber.live import LiveView

    view = LiveView(console=console)
    view.state.max_steps = max_steps

    def on_event(event: str, payload: dict) -> None:
        # Keep the panels that need session state fed from the observe event.
        if event == "observe":
            view.state.tabs = payload.get("tabs", [])
            view.state.clips = payload.get("clips", [])
        view.handle(event, payload)

    async with Chamber.open(cfg) as ch:
        with view:
            return await run_task(ch, task, start_url=url, on_event=on_event)


@app.command()
def see(
    url: Annotated[str, typer.Argument(help="Page to screenshot and describe.")],
    question: Annotated[str, typer.Option("--ask", "-a")] = "",
    model: Annotated[str, typer.Option("--model", "-m", help="Override CHAMBER_VISION_MODEL.")] = "",
    profile: Annotated[str, typer.Option("--profile", "-p")] = "vision",
    scale: Annotated[
        float, typer.Option("--scale", help="Shrink the screenshot before sending. 1.0 = full size.")
    ] = 0.0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Screenshot a page and have the vision model describe it.

    The way to benchmark a local VLM against real pages before trusting it in a
    run — it reports how long the model took, which is the number that decides
    whether it is usable on a laptop.
    """
    _setup_logging(verbose)
    from chamber.config import ModelConfig

    cfg = ChamberConfig.from_env(profile=profile)
    if model:
        base = cfg.vision or ModelConfig()
        cfg = ChamberConfig.from_env(
            profile=profile,
            vision=ModelConfig(
                provider="openai",
                model=model,
                base_url=base.base_url or "http://localhost:11434/v1",
                api_key=base.api_key or "ollama",
                max_tokens=512,
                request_timeout_s=180.0,
            ),
        )
    if not cfg.vision:
        console.print(
            "[red]No vision model configured.[/red] Set CHAMBER_VISION_MODEL, or pass "
            "--model (e.g. [bold]--model qwen2.5vl:3b[/bold])."
        )
        raise typer.Exit(1)

    if scale > 0:
        from dataclasses import replace

        cfg = replace(cfg, loop=replace(cfg.loop, vision_scale=scale))
    run_async(_see(cfg, url, question))


async def _see(cfg: ChamberConfig, url: str, question: str) -> None:
    import time as _time

    async with Chamber.open(cfg) as ch:
        console.print(
            f"[dim]{ch.build.label} · vision {cfg.vision.model} · "
            f"scale {cfg.loop.vision_scale}[/dim]"
        )
        await ch.goto(url)
        started = _time.monotonic()
        path, description = await ch.look(question)
        elapsed = _time.monotonic() - started
        size_kb = path.stat().st_size / 1024 if path.exists() else 0
        console.print(
            Panel(description, title=f"{cfg.vision.model} · {elapsed:.1f}s · {size_kb:.0f} KB")
        )
        console.print(f"[dim]{path}[/dim]")


@app.command()
def profiles(
    remove: Annotated[
        str, typer.Option("--remove", help="Delete a profile and everything in it.")
    ] = "",
    prune: Annotated[
        bool, typer.Option("--prune", help="Delete every profile except 'default'.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """List browser profiles with their sizes, or delete ones you are done with.

    A profile is a whole browser profile — cookies, logins, extension settings,
    history, and any preference you changed by hand. They persist across runs and
    are never touched between them, which is the point: log in once with
    `chamber open --profile dev` and every later run inherits the session.

    The cost is disk. A working profile runs 100-300 MB, so a handful of one-off
    `--profile` names adds up quickly.
    """
    import shutil

    root = paths.home() / "profiles"
    found = sorted(p for p in root.glob("*") if p.is_dir())

    def size_mb(path) -> float:
        total = 0
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
        return total / 1_048_576

    if remove or prune:
        targets = (
            [p for p in found if p.name != "default"] if prune
            else [p for p in found if p.name == remove]
        )
        if not targets:
            console.print("[yellow]nothing matched[/yellow]")
            raise typer.Exit(1)

        total = sum(size_mb(p) for p in targets)
        console.print(
            f"about to delete [bold]{len(targets)}[/bold] profile(s), "
            f"[bold]{total:.0f} MB[/bold]: {', '.join(p.name for p in targets)}"
        )
        console.print("[dim]this removes their cookies and logins permanently[/dim]")
        if not yes and not typer.confirm("delete them?"):
            console.print("nothing deleted")
            raise typer.Exit(0)

        for p in targets:
            shutil.rmtree(p, ignore_errors=True)
            console.print(f"  removed {p.name}")
        console.print(f"\n[green]freed about {total:.0f} MB[/green]")
        raise typer.Exit(0)

    if not found:
        console.print("[dim]no profiles yet — one is created on the first run[/dim]")
        raise typer.Exit(0)

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("profile")
    table.add_column("size", justify="right")
    table.add_column("logins in")
    total = 0.0
    for p in found:
        mb = size_mb(p)
        total += mb
        # Cookies is the file that makes a profile worth keeping.
        cookies = p / "Default" / "Cookies"
        has = "yes" if cookies.exists() and cookies.stat().st_size > 20_000 else "—"
        table.add_row(p.name, f"{mb:,.0f} MB", has)
    console.print(table)
    console.print(f"\n[dim]{len(found)} profiles, {total:,.0f} MB total — {root}[/dim]")
    console.print("[dim]chamber profiles --prune   removes all but 'default'[/dim]")


@app.command()
def trace(
    run_id: Annotated[str, typer.Argument(help="Run id. Omit to list recent runs.")] = "",
    sources: Annotated[bool, typer.Option("--sources", help="Just the source list.")] = False,
    llm: Annotated[
        bool,
        typer.Option(
            "--llm",
            help="Full model prompts, returned output, tool calls and API reasoning summaries.",
        ),
    ] = False,
) -> None:
    """Show what a past run actually did, step by step, with its sources."""
    from chamber.trace.store import open_store

    with open_store() as store:
        if not run_id:
            runs = store.runs()
            if not runs:
                console.print("[dim]no runs recorded yet[/dim]")
                return
            table = Table(box=None, padding=(0, 2))
            table.add_column("run")
            table.add_column("")
            table.add_column("task", overflow="ellipsis", max_width=60)
            table.add_column("steps", justify="right")
            for r in runs:
                mark = "[green]✓[/green]" if r["success"] else "[yellow]•[/yellow]"
                n = len(store.steps(r["id"]))
                table.add_row(r["id"], mark, r["task"], str(n))
            console.print(table)
            console.print("\n[dim]chamber trace <run-id>[/dim]")
            return

        if sources:
            found = store.sources(run_id)
            if not found:
                console.print("[dim]no pages recorded for that run[/dim]")
                return
            for s in found:
                star = "[yellow]★[/yellow]" if s["used"] else " "
                console.print(f"{star} {s['canonical']}  [dim]{s['title'] or ''}[/dim]")
            return

        if llm:
            console.print(store.model_transcript(run_id), markup=False)
            return

        console.print(store.timeline(run_id))


@app.command()
def setup(
    profile: Annotated[str, typer.Option("--profile", "-p")] = "default",
    url: Annotated[str, typer.Argument()] = "brave://settings/appearance",
    install: Annotated[
        bool, typer.Option("--install", help="Install uBlock Origin first.")
    ] = False,
) -> None:
    """Open the browser for YOU, with the agent switched off.

    A normal window on a chamber profile, started with the human holding control:
    no model is connected, nothing observes the page, and nothing will move until
    you hand it back. Configure the browser exactly how you want it — strip the
    bloat, sign in, set up extensions — and it is all saved to the profile that
    every later agent run uses.
    """
    _setup_logging(False)

    if install:
        import subprocess

        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "install_ubo.py"
        console.print("[bold]installing uBlock Origin…[/bold]")
        subprocess.run([sys.executable, str(script)], check=False)
        console.print()

    cfg = ChamberConfig.from_env(profile=profile)
    run_async(_setup(cfg, url))


async def _setup(cfg: ChamberConfig, url: str) -> None:
    async with Chamber.open(cfg) as ch:
        # Start held. Nothing in chamber acts while this is set, so the window is
        # genuinely yours — the bar says so, and the button offers it back.
        await ch.take_control(True)
        if url and url != "about:blank":
            with contextlib.suppress(Exception):
                await ch.page.goto(url, wait_until="domcontentloaded", timeout=20_000)

        console.print()
        console.print(
            Panel(
                "The browser is yours. No model is connected and nothing is watching "
                "the page.\n\n"
                "Change anything you like — settings, extensions, sign-ins, "
                "appearance. It is all written to the profile, and every later agent "
                "run inherits it.\n\n"
                "[dim]Close the window, or press Ctrl+C here, when you are done.[/dim]",
                title=f"[bold]setup · profile '{cfg.profile}'[/bold]",
                border_style="yellow",
            )
        )
        console.print(f"[dim]{paths.profile_dir(cfg.profile)}[/dim]\n")

        tips = Table(show_header=False, box=None, padding=(0, 2))
        tips.add_column(style="dim")
        tips.add_column()
        tips.add_row("appearance", "brave://settings/appearance")
        tips.add_row("new tab", "brave://settings/newTab")
        tips.add_row("extensions", "brave://extensions")
        tips.add_row("shields", "brave://settings/shields")
        tips.add_row("kept for you", "chamber only resets the startup page; the rest is yours")
        console.print(tips)
        console.print()

        try:
            while ch.tab_ids():
                await asyncio.sleep(1)
            console.print("[green]window closed — your changes are saved to the profile[/green]")
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\n[green]saving and closing…[/green]")


if __name__ == "__main__":
    app()
