"""LangSmith traces for assessment workers; state, evidence, and spending stay authoritative.

Each worker is one root trace so a finished question or report becomes one dataset example.
Configure `LANGSMITH_API_KEY` plus either `LANGSMITH_PROJECT` or the agent address pair
`LANGSMITH_AGENT_ID`/`LANGSMITH_AGENT_ENVIRONMENT`. Addressing is a per-workspace beta;
a workspace without it rejects addressed runs, so select it only where it is enabled.
"""

import contextlib
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from langsmith import Client
from langsmith.address import AgentAddress, agent_address
from langsmith.run_helpers import trace, tracing_context
from langsmith.utils import LangSmithNotFoundError


@dataclass
class WorkerTrace:
    """A worker's root run: its inputs and accepted outputs form one dataset example."""

    trace_id: str | None = None
    run: object | None = None
    inputs: Mapping[str, object] | None = None
    metadata: Mapping[str, object] | None = None

    def outputs(self, values: Mapping[str, object]) -> None:
        if self.run is None:
            return
        try:
            self.run.add_outputs(dict(values))
        except Exception:
            logging.getLogger(__name__).warning("Worker trace outputs could not be recorded.")


class Tracing:
    def __init__(
        self,
        client: Client,
        *,
        release: str,
        project: str | None = None,
        address: AgentAddress | None = None,
        dataset: str | None = None,
    ):
        if (project is None) == (address is None):
            raise ValueError("Select exactly one LangSmith destination: a project or an agent.")
        self.client, self.release, self.dataset = client, release, dataset
        self.destination = {"address": address} if address else {"project_name": project}

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], *, release: str) -> "Tracing":
        if not environment.get("LANGSMITH_API_KEY"):
            raise ValueError("Missing Condere runtime configuration: LANGSMITH_API_KEY")
        agent = environment.get("LANGSMITH_AGENT_ID")
        stage = environment.get("LANGSMITH_AGENT_ENVIRONMENT")
        project = environment.get("LANGSMITH_PROJECT")
        if bool(agent) != bool(stage) or bool(agent) == bool(project):
            raise ValueError(
                "Configure LANGSMITH_PROJECT, or both LANGSMITH_AGENT_ID and "
                "LANGSMITH_AGENT_ENVIRONMENT, for Condere traces."
            )
        client = Client(
            api_url=environment.get("LANGSMITH_ENDPOINT") or None,
            api_key=environment["LANGSMITH_API_KEY"],
            workspace_id=environment.get("LANGSMITH_WORKSPACE_ID") or None,
            timeout_ms=10_000,
        )
        return cls(
            client,
            release=release,
            project=project or None,
            address=agent_address(agent, stage) if agent else None,
            dataset=environment.get("LANGSMITH_DATASET") or None,
        )

    @contextlib.contextmanager
    def worker(
        self,
        name: str,
        *,
        inputs: Mapping[str, object],
        metadata: Mapping[str, object],
        tags: list[str],
    ) -> Iterator[WorkerTrace]:
        """Yield the worker's root run; tracing failure leaves an empty handle and work proceeds."""
        stack, handle = contextlib.ExitStack(), WorkerTrace()
        try:
            stack.enter_context(
                tracing_context(enabled=True, client=self.client, **self.destination)
            )
            run = stack.enter_context(
                trace(
                    name,
                    "chain",
                    inputs=dict(inputs),
                    client=self.client,
                    metadata={**metadata, "method_version": self.release},
                    tags=tags,
                    **self.destination,
                )
            )
            handle = WorkerTrace(str(run.trace_id), run, inputs, metadata)
        except Exception:
            stack.close()
            stack = contextlib.ExitStack()
            logging.getLogger(__name__).warning("Worker tracing could not start.")
        with stack:
            yield handle

    def record(
        self,
        name: str,
        *,
        inputs: Mapping[str, object],
        outputs: Mapping[str, object],
        metadata: Mapping[str, object],
        tags: list[str],
        scores: Mapping[str, float | None] | None = None,
    ) -> str | None:
        """Record a finished non-agent step as its own root trace, with numeric feedback.

        Feedback is the signal Engine and online evaluators rank traces by, so a step's
        quality measures go there rather than only into its outputs.
        """
        try:
            with tracing_context(enabled=True, client=self.client, **self.destination):
                with trace(
                    name,
                    "chain",
                    inputs=dict(inputs),
                    client=self.client,
                    metadata={**metadata, "method_version": self.release},
                    tags=tags,
                    **self.destination,
                ) as run:
                    run.add_outputs(dict(outputs))
            self.client.flush(timeout=5)
            for key, score in (scores or {}).items():
                if score is not None:
                    self.client.create_feedback(run.id, key, score=score)
            return str(run.trace_id)
        except Exception:
            logging.getLogger(__name__).warning("Step trace %s could not be recorded.", name)
            return None

    def record_example(self, handle: WorkerTrace, outputs: Mapping[str, object]) -> None:
        """Add an accepted live answer to the candidate dataset, linked to its trace.

        Written by the application rather than a LangSmith rule: on 2026-10-28 the rule
        engine matched no successful runs although the same filter matched in queries.
        """
        if self.dataset is None or handle.run is None:
            return
        try:
            self.client.create_example(
                dataset_name=self.dataset,
                inputs=dict(handle.inputs or {}),
                outputs=dict(outputs),
                metadata={**(handle.metadata or {}), "method_version": self.release},
                source_run_id=handle.run.id,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Accepted answer could not be added to the dataset."
            )

    def status(self, trace_id: str) -> tuple[str, str | None]:
        """Report trace presence; a missing trace never shows that no work or spend occurred."""
        try:
            self.client.flush(timeout=5)
            self.client.read_run(trace_id)
            return "observed", None
        except LangSmithNotFoundError:
            return "pending", None
        except Exception as error:
            return "unavailable", type(error).__name__

    def close(self) -> None:
        self.client.flush(timeout=5)
