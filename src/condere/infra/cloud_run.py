"""Start one Cloud Run Job execution per assessment (the API stays small and scales to zero).

Runs inside Cloud Run, authenticating as the service's own identity through the metadata
server; that identity needs `run.jobs.runWithOverrides` on the job.
"""

import re
import time

import httpx

METADATA_TOKEN = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
RUN_API = "https://run.googleapis.com/v2"
JOB = re.compile(r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/jobs/[a-z0-9-]+")


class CloudRunJobs:
    def __init__(self, job: str, *, transport=None):
        """`job` is the full name: projects/<project>/locations/<region>/jobs/<job>."""
        if not JOB.fullmatch(job):
            raise ValueError("Name the job as projects/<project>/locations/<region>/jobs/<job>.")
        self.job = job
        self.client = httpx.AsyncClient(timeout=30, follow_redirects=False, transport=transport)
        self._token, self._expires = None, 0.0

    async def _access_token(self) -> str:
        if self._token is None or time.monotonic() > self._expires:
            response = await self.client.get(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
            response.raise_for_status()
            token = response.json()
            self._token = token["access_token"]
            self._expires = time.monotonic() + token["expires_in"] - 60
        return self._token

    async def __call__(self, owner: str, assessment_id: str, resume: bool) -> str:
        """Start the job for one assessment; returns the long-running operation name."""
        args = ["--owner", owner, "--assessment", assessment_id] + (["--resume"] if resume else [])
        response = await self.client.post(
            f"{RUN_API}/{self.job}:run",
            headers={"Authorization": f"Bearer {await self._access_token()}"},
            json={"overrides": {"containerOverrides": [{"args": args}], "taskCount": 1}},
        )
        response.raise_for_status()
        return response.json()["name"]

    async def close(self) -> None:
        await self.client.aclose()
