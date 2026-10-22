import io
import json
import tarfile

import httpx
import pytest

from condere.runtime.repositories import acquire_repository

REPOSITORY = "https://github.com/example/project"
COMMIT = "a" * 40


def archive_bytes(path="project/README.md"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        entry = tarfile.TarInfo(path)
        data = b"Recorded source content"
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/project",
        "https://github.com@127.0.0.1/project",
        "https://token@github.com/example/project",
        "https://github.com/example/project/tree/main",
        "https://github.com/example/project?next=http://localhost",
        "https://github.com/another/project",
    ],
)
def test_rejects_unadmitted_or_unsafe_destinations_before_network(url, tmp_path):
    def forbidden(_):
        pytest.fail("Rejected URLs must not issue requests")

    with (
        httpx.Client(transport=httpx.MockTransport(forbidden)) as client,
        pytest.raises(ValueError),
    ):
        acquire_repository(
            url, allowed_repositories={REPOSITORY}, destination=tmp_path, client=client
        )


def test_pins_original_and_reuses_saved_snapshot(tmp_path):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=[{"sha": COMMIT}])
        return httpx.Response(200, content=archive_bytes())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        snapshot = acquire_repository(
            REPOSITORY, allowed_repositories={REPOSITORY}, destination=tmp_path, client=client
        )
        saved = acquire_repository(
            REPOSITORY,
            allowed_repositories={REPOSITORY},
            destination=tmp_path,
            commit=COMMIT,
            client=client,
        )
        assert snapshot == saved
        assert len(requests) == 2
        assert snapshot.entries[0]["path"] == "README.md"
        record = json.loads((snapshot.archive.parent / "snapshot.json").read_text())
        assert record["commit"] == COMMIT
        snapshot.archive.write_bytes(b"changed")
        with pytest.raises(ValueError, match="inconsistent"):
            acquire_repository(
                REPOSITORY,
                allowed_repositories={REPOSITORY},
                destination=tmp_path,
                commit=COMMIT,
                client=client,
            )


def test_rejects_archive_traversal_without_host_extraction(tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=archive_bytes("../escape"))
        )
    ) as client:
        with pytest.raises(ValueError, match="Unsafe path"):
            acquire_repository(
                REPOSITORY,
                allowed_repositories={REPOSITORY},
                destination=tmp_path,
                commit=COMMIT,
                client=client,
            )
    assert not (tmp_path.parent / "escape").exists()
    assert not list(tmp_path.rglob("source.tar.gz"))


def test_does_not_follow_redirects_outside_github(tmp_path):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    with (
        httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        acquire_repository(
            REPOSITORY,
            allowed_repositories={REPOSITORY},
            destination=tmp_path,
            commit=COMMIT,
            client=client,
        )
    assert len(requests) == 1
