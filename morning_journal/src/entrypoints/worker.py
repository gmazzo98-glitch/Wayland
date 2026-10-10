"""Worker entrypoint for the Wayland Morning Journal workflow."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from mistralai.workflows import run_worker
except ImportError:
    import mistralai_workflows

    run_worker = mistralai_workflows.run_worker

from src.workflows.journal import WaylandMorningJournal


async def main() -> None:
    await run_worker([WaylandMorningJournal])


if __name__ == "__main__":
    asyncio.run(main())
