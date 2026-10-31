"""Run one admitted assessment to completion: the Cloud Run Job entry point.

The API starts one execution per assessment with `--owner` and `--assessment` (and
`--resume` to continue). The job uses the same configuration as the API.
"""

import argparse
import asyncio
import os
import sys

from condere.interfaces.server import build_runtime, configuration
from condere.runtime.coordinator import run_recorded


async def run(values: dict, owner: str, assessment_id: str, resume: bool) -> None:
    service, coordinator, objects, tracing = build_runtime(values)
    try:
        await run_recorded(coordinator, service.state, owner, assessment_id, resume=resume)
    finally:
        objects.close()
        # A job's container exits when this returns; flush traces first or they are lost.
        await asyncio.to_thread(tracing.close)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--assessment", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        values = configuration(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    asyncio.run(run(values, args.owner, args.assessment, args.resume))


if __name__ == "__main__":
    main(sys.argv[1:])
