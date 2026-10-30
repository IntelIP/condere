"""Intake, citation and repository-inspection contracts shared by every interface.

These checks establish structure and references; whether cited evidence supports a
claim is judged in the analysis and decision.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, StringConstraints, model_validator

from condere.runtime.calculations import Record, Text


class Citation(Record):
    """Cite by source ID and exact quoted text; the application resolves hash and offsets."""

    model_config = {"extra": "forbid", "str_strip_whitespace": False}
    source_id: Text
    quote: Annotated[str, Field(min_length=1, max_length=12_000)]
    text_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def bounded_window(self) -> Self:
        if self.start is not None and self.end is not None:
            if self.end <= self.start or self.end - self.start > 12_000:
                raise ValueError("A citation must name a nonempty, bounded source window.")
        return self


class RepositoryInspection(Record):
    repository_url: Text
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    archive_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    archive_source_id: Text
    machine_id: Text
    receipt_id: Text
    network_enabled: Literal[False]
    inspected_files: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]]
    limitations: list[Text] = Field(default_factory=list)


class IntakeBrief(Record):
    original_input: Annotated[Text, StringConstraints(strip_whitespace=False)]
    supplied_facts: list[Text] = Field(default_factory=list)
    technology: Text | None = None
    idea: Text | None = None
    public_references: list[HttpUrl] = Field(default_factory=list)
    source_ids: list[Text] = Field(default_factory=list)
    customer_hypotheses: list[Text] = Field(default_factory=list)
    founder_resources: list[Text] = Field(default_factory=list)
    constraints: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def no_reference_credentials(self) -> Self:
        if not self.original_input.strip():
            raise ValueError("The original input cannot be blank.")
        if any(url.username or url.password for url in self.public_references):
            raise ValueError("Public references cannot include credentials.")
        return self
