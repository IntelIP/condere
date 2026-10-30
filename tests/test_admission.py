"""A repository the supplied starting URL links to can be inspected without a separate search."""

from condere.runtime.repositories import canonical_repository
from condere.runtime.tools import _linked_from_start

POST = "https://x.com/opinion_bits/status/2114757538424217717"
REPOSITORY = "https://github.com/StarTrail-org/LEANN"
CANONICAL = canonical_repository(REPOSITORY)  # The tool compares canonical forms.


def source(origin: str, links: list[str]) -> dict:
    return {
        "payload": {
            "id": "a" * 64,
            "kind": "web",
            "origin": origin,
            "retrieved_at": "2026-10-30T12:00:00+00:00",
            "media_type": "application/json",
            "original_sha256": "b" * 64,
            "text_sha256": "c" * 64,
            "original_bytes": 10,
            "text_bytes": 10,
            "provenance": {"provider": "apify", "links": links},
            "limitations": [],
        }
    }


def test_links_of_the_starting_url_admit_their_repository():
    brief = {"public_references": [POST.replace("x.com", "twitter.com")]}
    records = [source(POST, [REPOSITORY + "/tree/main/apps", "https://t.co/xyz"])]
    assert _linked_from_start(brief, records, CANONICAL)["basis"].startswith("linked from")


def test_links_of_other_pages_do_not():
    brief = {"public_references": [POST]}
    records = [source("https://example.com/blog", [REPOSITORY])]
    assert _linked_from_start(brief, records, CANONICAL) is None
    assert (
        _linked_from_start({"public_references": []}, [source(POST, [REPOSITORY])], CANONICAL)
        is None
    )
