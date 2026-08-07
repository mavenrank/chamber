"""Comparison shopping — find a thing, compare prices, stop before buying.

    uv run python demos/shopping.py "usb-c hub with ethernet under 40 pounds"

Two things this demo is really testing:

**Tables.** Price and spec information is tabular, and `dom/reader.py` renders real
markdown tables precisely so the model can see which price belongs to which
product. Flatten that and the task becomes guesswork.

**Where to stop.** The agent will add things to a basket. It will not check out, and
it will not touch a payment field — `interrupt/detect.py` classifies a card form as
a `payment` challenge and the prompt below makes the boundary explicit. Buying
something is the user's decision, and an agent that makes it for them is a bug no
matter how good its price comparison was.
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

SHOPPING_BRIEF = """\
# This task is comparison shopping

- **Compare at least three options** before recommending one, and check more than
  one retailer if you can. The cheapest listing on one site is not the cheapest
  price.
- **Report the total.** Delivery and any surcharge are part of the price. If you
  cannot see the delivery cost without checking out, say so rather than guessing.
- **Record what you compared,** not just what you picked — the alternatives and
  their prices are most of the value of the answer.

## Where you stop

You may search, filter, sort, open listings and add items to a basket.

You may **not** check out, enter payment details, enter an address, or confirm an
order. If the page asks for a card, that is the end of your part: call `ask_human`
with `reason: "payment"` and let the person decide. The same goes for signing in.

This is not a limitation to work around. Spending someone's money is their
decision, and handing it back at the right moment is the job.
"""


async def main() -> int:
    if len(sys.argv) < 2:
        console.print('usage: uv run python demos/shopping.py "what you are looking for"')
        return 2
    want = " ".join(sys.argv[1:])

    task = (
        f"Find the best value option for: {want}. Compare at least three, then "
        "recommend one and explain why. Include the price and the link for each "
        "option you considered."
    )

    cfg = ChamberConfig.from_env(profile="shopping", loop__max_steps=30)
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
            if payload["name"] == "ask_human":
                console.print("  [yellow]→ handed the window over to you[/yellow]")

    with open_store() as store:
        async with Chamber.open(cfg) as ch:
            console.print(Panel(want, title="shopping for", border_style="cyan"))
            console.print(
                "[dim]the agent may add to a basket; it will never check out[/dim]\n"
            )
            async with LLM(cfg.model) as llm:
                agent = Agent(ch, llm, on_event=on_event, store=store, system_extra=SHOPPING_BRIEF)
                result = await agent.run(task)

            console.print()
            console.print(Panel(result.summary, title="recommendation", border_style="green"))

            shops = store.sources(ch.run_id)
            console.print(f"\n[bold]pages checked ({len(shops)})[/bold]")
            for shop in shops:
                console.print(f"  · {shop['canonical'][:80]}")

            console.print(f"\n[dim]full trace: chamber trace {ch.run_id}[/dim]")
            console.print("[dim]browser stays open for 30s — check the basket yourself[/dim]")
            await ch.overlay.think(
                thought="Done. Nothing was purchased — the basket is yours to review.",
                status="ok",
            )
            await asyncio.sleep(30)

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
