"""The Cloud Run Jobs launcher, with all Google traffic simulated."""

import json

import httpx
import pytest

from condere.infra.cloud_run import CloudRunJobs

JOB = "projects/example-project/locations/us-east1/jobs/condere-assessment"


async def test_each_assessment_starts_one_job_execution_with_its_arguments():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "synthetic", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"name": "operations/run-1"})

    launcher = CloudRunJobs(JOB, transport=httpx.MockTransport(respond))
    try:
        assert await launcher("owner-1", "case-1", False) == "operations/run-1"
        await launcher("owner-1", "case-1", True)
    finally:
        await launcher.close()
    runs = [request for request in requests if request.url.host == "run.googleapis.com"]
    assert [str(request.url) for request in runs] == [
        f"https://run.googleapis.com/v2/{JOB}:run"
    ] * 2
    first, second = (json.loads(request.content)["overrides"] for request in runs)
    assert first == {
        "containerOverrides": [{"args": ["--owner", "owner-1", "--assessment", "case-1"]}],
        "taskCount": 1,
    }
    assert second["containerOverrides"][0]["args"][-1] == "--resume"
    # The access token is reused until it nears expiry.
    assert sum(request.url.host == "metadata.google.internal" for request in requests) == 1


def test_the_job_must_be_named_in_full():
    with pytest.raises(ValueError, match="projects/"):
        CloudRunJobs("condere-assessment")
