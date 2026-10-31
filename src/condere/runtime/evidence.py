"""Immutable source objects and authenticated retrieval independent of VMs/tracing.

The S3 client is for the selected local object-store integration. GCS/hosted region
verification remains a separate deployment step; this module never provisions it.
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import AwareDatetime, Field

from condere.runtime.calculations import Record, Text
from condere.runtime.contracts import Citation
from condere.runtime.state import AssessmentStore, NotFound, content_hash

MAX_OBJECT_BYTES = 80 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EvidenceError(Exception):
    """Missing, oversized, or changed evidence must not appear as a supported finding."""


class SourceDocument(Record):
    id: Digest
    kind: Literal[
        "web",
        "repository",
        "repository_index",
        "repository_metadata",
        "user_file",
        "measurement",
    ]
    origin: Text
    retrieved_at: AwareDatetime
    media_type: Text
    original_sha256: Digest
    text_sha256: Digest
    original_bytes: int = Field(ge=0, le=MAX_OBJECT_BYTES)
    text_bytes: int = Field(ge=0, le=MAX_TEXT_BYTES)
    provenance: dict
    limitations: list[Text]


def object_client(environment: dict[str, str]):
    """Use explicit dedicated credentials; never fall back to ambient AWS discovery."""
    endpoint = environment["CONDERE_EVIDENCE_ENDPOINT"]
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Evidence endpoint must not include credentials, a query, or a fragment.")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("Evidence endpoints require HTTPS, except loopback development.")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=environment["CONDERE_EVIDENCE_REGION"],
        aws_access_key_id=environment["CONDERE_EVIDENCE_ACCESS_KEY"],
        aws_secret_access_key=environment["CONDERE_EVIDENCE_SECRET_KEY"],
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
            # Cloudflare R2 rejects the CRC checksums newer botocore sends by default; send and
            # check them only where an operation requires them (also valid for MinIO and S3).
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


class EvidenceStore:
    def __init__(self, state: AssessmentStore, client, bucket: str):
        if not bucket:
            raise ValueError("Assessment evidence requires a dedicated bucket.")
        self.state, self.client, self.bucket = state, client, bucket

    @staticmethod
    def _key(assessment_id: str, digest: str) -> str:
        return f"assessments/{assessment_id}/objects/{digest}"

    def _read_object(self, assessment_id: str, digest: str, limit: int) -> bytes:
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self._key(assessment_id, digest)
            )
        except ClientError as error:
            raise EvidenceError("The saved evidence object could not be retrieved.") from error
        stream = response["Body"]
        try:
            if response["ContentLength"] > limit:
                raise EvidenceError("The saved object exceeds the permitted retrieval size.")
            body = stream.read(limit + 1)
            if len(body) > limit or len(body) != response["ContentLength"]:
                raise EvidenceError("Saved object length changed or exceeds the retrieval size.")
        finally:
            stream.close()
        if hashlib.sha256(body).hexdigest() != digest:
            raise EvidenceError("Saved evidence does not match its original content hash.")
        return body

    def _put_object(self, assessment_id: str, body: bytes, media_type: str) -> str:
        if len(body) > MAX_OBJECT_BYTES:
            raise EvidenceError("The source exceeds the admitted object size.")
        digest = hashlib.sha256(body).hexdigest()
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(assessment_id, digest),
                Body=body,
                ContentType=media_type,
                IfNoneMatch="*",
                Metadata={"sha256": digest},
            )
        except ClientError as error:
            if str(error.response.get("Error", {}).get("Code")) not in {
                "PreconditionFailed",
                "412",
            }:
                raise EvidenceError("The original source object could not be saved.") from error
            if self._read_object(assessment_id, digest, MAX_OBJECT_BYTES) != body:
                raise EvidenceError("An existing source object cannot be replaced.") from error
        return digest

    def save_source(
        self,
        owner: str,
        assessment_id: str,
        *,
        kind: str,
        origin: str,
        original: bytes,
        text: str,
        media_type: str,
        provenance: dict,
        limitations: list[str],
        retrieved_at: datetime | None = None,
        expected_id: str | None = None,
    ) -> SourceDocument:
        self.state.get(owner, assessment_id)  # Authenticate before any object-store access.
        text_bytes = text.encode("utf-8")
        if len(text_bytes) > MAX_TEXT_BYTES:
            raise EvidenceError(
                "Extracted source text exceeds its size limit; retain a bounded excerpt."
            )
        timestamp = retrieved_at or datetime.now(UTC)
        metadata = dict(
            kind=kind,
            origin=origin,
            retrieved_at=timestamp,
            media_type=media_type,
            original_sha256=hashlib.sha256(original).hexdigest(),
            text_sha256=hashlib.sha256(text_bytes).hexdigest(),
            original_bytes=len(original),
            text_bytes=len(text_bytes),
            provenance=provenance,
            limitations=limitations,
        )
        source = SourceDocument(
            id=content_hash(metadata | {"retrieved_at": timestamp.isoformat()}), **metadata
        )
        if expected_id is not None and source.id != expected_id:
            raise EvidenceError("A copied source would not keep its original identity.")
        self._put_object(assessment_id, original, media_type)
        self._put_object(assessment_id, text_bytes, "text/plain; charset=utf-8")
        self.state.append_record(
            owner, assessment_id, "source", source.id, source.model_dump(mode="json")
        )
        return source

    def copy_source(
        self, owner: str, from_assessment: str, to_assessment: str, source_id: str
    ) -> SourceDocument:
        """Save one owner's source into another of their assessments under the same identity.

        The original bytes and text are read back through their hash checks and saved through
        `save_source` with identical metadata, so the ID, and every quote against it, still hold.
        """
        source = self.source(owner, from_assessment, source_id)
        return self.save_source(
            owner,
            to_assessment,
            kind=source.kind,
            origin=source.origin,
            original=self.original(owner, from_assessment, source_id),
            text=self.text(owner, from_assessment, source_id),
            media_type=source.media_type,
            provenance=source.provenance,
            limitations=source.limitations,
            retrieved_at=source.retrieved_at,
            expected_id=source_id,
        )

    def source(self, owner: str, assessment_id: str, source_id: str) -> SourceDocument:
        for record in self.state.records(owner, assessment_id, kind="source"):
            if record["record_key"] == source_id:
                source = SourceDocument.model_validate(record["payload"])
                if (
                    content_hash(record["payload"]) != record["content_hash"]
                    or source.id != source_id
                ):
                    raise EvidenceError("Saved source metadata failed its identity check.")
                return source
        raise NotFound("Source not found in this assessment.")

    def original(self, owner: str, assessment_id: str, source_id: str) -> bytes:
        source = self.source(owner, assessment_id, source_id)
        return self._read_object(assessment_id, source.original_sha256, MAX_OBJECT_BYTES)

    def text(self, owner: str, assessment_id: str, source_id: str) -> str:
        source = self.source(owner, assessment_id, source_id)
        body = self._read_object(assessment_id, source.text_sha256, MAX_TEXT_BYTES)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EvidenceError("Saved source text is not valid UTF-8.") from error

    def passage(
        self, owner: str, assessment_id: str, source_id: str, start: int = 0, limit: int = 4_000
    ) -> dict:
        if start < 0 or limit < 1 or limit > 12_000:
            raise ValueError("Request a nonnegative offset and 1-12000 characters.")
        source = self.source(owner, assessment_id, source_id)
        text = self.text(owner, assessment_id, source_id)
        if start >= len(text):
            raise ValueError("The requested source window starts beyond the saved text.")
        end = min(len(text), start + limit)
        citation = Citation(
            source_id=source_id,
            text_sha256=source.text_sha256,
            start=start,
            end=end,
            quote=text[start:end],
        )
        return {
            "citation": citation.model_dump(),
            "next_offset": end if end < len(text) else None,
            "total_characters": len(text),
        }

    def resolve_citations(self, owner: str, assessment_id: str, payload):
        """Fill each citation's hash and offsets from its source ID and exact quoted text.

        Models quote evidence; code locates it. Whitespace may differ between the quote and
        the source, but the saved citation always carries the source's exact text.
        """
        if isinstance(payload, list):
            return [self.resolve_citations(owner, assessment_id, item) for item in payload]
        if not isinstance(payload, dict):
            return payload
        resolved = {}
        for key, value in payload.items():
            if key == "citations" and isinstance(value, list):
                resolved[key] = [self._resolve(owner, assessment_id, item) for item in value]
            else:
                resolved[key] = self.resolve_citations(owner, assessment_id, value)
        return resolved

    def _resolve(self, owner: str, assessment_id: str, citation):
        if not isinstance(citation, dict) or not isinstance(citation.get("quote"), str):
            return citation
        source_id, quote = citation.get("source_id"), citation["quote"]
        source = self.source(owner, assessment_id, source_id)
        text = self.text(owner, assessment_id, source_id)
        start = citation.get("start")
        if isinstance(start, int) and 0 <= start and text[start : start + len(quote)] == quote:
            match = (start, start + len(quote))
        elif (index := text.find(quote)) >= 0:
            match = (index, index + len(quote))
        else:
            words = quote.split()
            found = (
                re.search(r"\s+".join(re.escape(word) for word in words), text) if words else None
            )
            if found is None:
                raise EvidenceError(
                    f"The quoted text was not found in source {source_id}. Copy an exact "
                    "passage from read_source or search_saved_sources."
                )
            match = found.span()
        return citation | {
            "text_sha256": source.text_sha256,
            "start": match[0],
            "end": match[1],
            "quote": text[match[0] : match[1]],
        }

    def verify_citation(self, owner: str, assessment_id: str, citation: Citation) -> None:
        source = self.source(owner, assessment_id, citation.source_id)
        text = self.text(owner, assessment_id, citation.source_id)
        if (
            citation.text_sha256 is None
            or citation.start is None
            or citation.end is None
            or source.text_sha256 != citation.text_sha256
            or citation.end > len(text)
            or text[citation.start : citation.end] != citation.quote
        ):
            raise EvidenceError("A cited passage does not match its saved source.")
