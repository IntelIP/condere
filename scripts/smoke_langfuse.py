"""Send/retrieve a synthetic trace, or retrieve one again after a service restart."""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from langfuse import Langfuse


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace-id", help="Verify a previously saved trace without sending another"
    )
    args = parser.parse_args()
    base_url = os.environ["LANGFUSE_BASE_URL"]
    if urlsplit(base_url).hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("This local smoke test requires the dedicated loopback Langfuse instance.")
    client = Langfuse(environment="development", release="condere-mda-infrastructure")
    trace_id = args.trace_id
    try:
        if not client.auth_check():
            raise SystemExit("Local Langfuse authentication failed.")
        if not trace_id:
            with client.start_as_current_observation(
                name="condere.infrastructure.synthetic",
                as_type="span",
                metadata={
                    "simulated": True,
                    "model_invoked": False,
                    "milestone": "MDA-01",
                    "assessment_id": str(uuid4()),
                },
                input={"purpose": "Verify trace persistence without inference"},
            ) as span:
                trace_id = client.get_current_trace_id()
                span.update(output={"diagnostic": "ready"})
            client.flush()
        deadline = time.monotonic() + 90
        last_error = None
        while time.monotonic() < deadline:
            try:
                observations = client.api.observations.get_many(
                    trace_id=trace_id, fields="core,basic,metadata", limit=10
                )
                matching = [
                    item
                    for item in observations.data
                    if item.trace_id == trace_id and item.name == "condere.infrastructure.synthetic"
                ]
                if not matching:
                    raise RuntimeError("Diagnostic observation is not yet queryable.")
                record = {
                    "assessment_id": matching[0].metadata["assessment_id"],
                    "trace_id": trace_id,
                    "observation_id": matching[0].id,
                    "base_url": base_url,
                    "retrieved": True,
                    "after_restart": bool(args.trace_id),
                    "simulated": True,
                    "model_invoked": False,
                }
                output = Path(".artifacts/infrastructure")
                output.mkdir(parents=True, exist_ok=True)
                name = "langfuse-after-restart.json" if args.trace_id else "langfuse-trace.json"
                (output / name).write_text(json.dumps(record, indent=2) + "\n")
                print(json.dumps(record))
                return
            except Exception as exc:
                last_error = type(exc).__name__
                time.sleep(2)
        raise SystemExit(f"Trace was not retrievable within the ingestion check: {last_error}.")
    finally:
        client.shutdown()


if __name__ == "__main__":
    main()
