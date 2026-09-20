"""Chamber — a headed browser an LLM can drive in the open.

The public surface is deliberately small. Everything an embedder needs:

    from chamber import Chamber, ChamberConfig

    async with Chamber.open(ChamberConfig(profile="research")) as ch:
        await ch.goto("https://example.com")
        view = await ch.observe()          # compact, model-ready page state
        await ch.act({"action": "click", "ref": "e12"})
"""

from chamber.config import ChamberConfig
from chamber.session import Chamber

__all__ = ["Chamber", "ChamberConfig", "__version__"]
__version__ = "0.11.0"
