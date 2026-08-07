"""Research with a visible trail.

    uv run python demos/research.py "why did Chrome drop Manifest V2"

The point of doing this in a browser rather than through a search API is the trail.
Every page opened is recorded, in order, with what came out of it, and the ones the
answer actually cites are marked — so the output is a claim *plus* the route to it,
and you can click back through the route yourself.

The system prompt addition below is the whole customisation. Everything else — the
loop, the refs, the repair pass, the captcha handoff — is the same machinery any
other task uses.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rich.console import Console
from rich.panel import Panel

from chamber import Chamber, ChamberConfig
from chamber.agent.llm import LLM
from chamber.agent.loop import Agent
from chamber.trace.store import open_store

console = Console()

RESEARCH_BRIEF = """\
# This task is research

You are answering a question by reading real pages, not from memory. Rules that
apply on top of the general ones:

- **Use at least two independent sources.** A claim that appears on one page and
  nowhere else is worth reporting as exactly that.
- **Read the page, do not recall it.** If the page content section does not contain
  the fact, say so and go find a page that does. Answering from prior knowledge
  defeats the entire point of this run.
- **Prefer primary sources.** A changelog, a spec, a vendor's own announcement or a
  bug tracker beats a news article summarising one.
- **Quote URLs in your final summary.** Any URL you cite is recorded as a source
  that fed the answer, as distinct from a page you merely opened.
- **Say what you could not confirm.** An answer with a stated gap is more useful
  than a confident one that papers over it.

Search with DuckDuckGo (https://duckduckgo.com/?q=...) rather than Google — it
serves results without a bot check far more often, which saves you a handoff.
"""


async def main() -> int:
    if len(sys.argv) < 2:
        console.print('usage: uv run python demos/research.py "your question"')
        return 2
    question = " ".join(sys.argv[1:])

    cfg = ChamberConfig.from_env(profile="research", loop__max_steps=30)
    if not cfg.model.api_key:
        console.print("[red]No API key — copy .env.example to .env first.[/red]")
        return 1

    def on_event(event: str, payload: dict) -> None:
        if event == "observe":
            console.print(f"\n[cyan]step {payload['step']}[/cyan] [dim]{payload['url'][:80]}[/dim]")
        elif event == "thought" and payload.get("text"):
            console.print(f"  [italic]{payload['text']}[/italic]")
        elif event == "action":
            mark = "[green]✓[/green]" if payload["ok"] else "[red]✗[/red]"
            console.print(f"  {mark} {payload['name']} [dim]{payload['detail'][:90]}[/dim]")

    with open_store() as store:
        async with Chamber.open(cfg) as ch:
            console.print(Panel(question, title="researching", border_style="cyan"))
            async with LLM(cfg.model) as llm:
                agent = Agent(ch, llm, on_event=on_event, store=store, system_extra=RESEARCH_BRIEF)
                result = await agent.run(question)

            console.print()
            console.print(Panel(result.summary, title="answer", border_style="green"))

            sources = store.sources(ch.run_id)
            console.print(f"\n[bold]trail — {len(sources)} pages opened[/bold]")
            for source in sources:
                star = "[yellow]★ cited[/yellow]" if source["used"] else "[dim]  seen [/dim]"
                title = (source["title"] or "")[:50]
                console.print(f"  {star} {source['canonical'][:78]}")
                if title:
                    console.print(f"          [dim]{title}[/dim]")

            console.print(f"\n[dim]full trace: chamber trace {ch.run_id}[/dim]")
            console.print("[dim]the browser stays open for 20s so you can retrace it[/dim]")
            await ch.overlay.think(
                thought="Research finished. The pages I used are still in history.",
                status="ok",
            )
            await asyncio.sleep(20)

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
