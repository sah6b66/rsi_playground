"""Changeset data structures (DD-09 §3).

A :class:`Changeset` is produced by the optimizer applier and consumed by
``gitstore.commit_candidate``. It describes file-level create/edit/delete
operations relative to ``suts/<sut_id>/`` plus provenance metadata.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


try:
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema, core_schema
except ImportError:  # pragma: no cover
    core_schema = None  # type: ignore[assignment]


class VersionId(str):
    """Git commit SHA (40-char hex) used as an immutable version identifier."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: type, handler: "GetCoreSchemaHandler"
    ) -> "CoreSchema":
        return core_schema.no_info_after_validator_function(VersionId, core_schema.str_schema())


class FileChange(BaseModel):
    """A single file-level operation within a Changeset."""

    path: str = Field(description="Relative path inside suts/<sut_id>/")
    op: Literal["create", "edit", "delete"]
    content: str | None = Field(
        default=None, description="Full text for create/edit; None for delete"
    )
    target: str = Field(description="Optimization target (TargetRegistry key)")
    declared: bool = Field(
        description="Whether the declaration matches the actual diff"
    )


class Changeset(BaseModel):
    """A set of file changes to apply on top of a base version."""

    hypothesis_id: str
    base_version: VersionId
    files: list[FileChange] = Field(default_factory=list)


class FileDiff(BaseModel):
    """One file-level difference between two versions (DD-09 §3)."""

    path: str
    op: Literal["create", "edit", "delete"]
    patch: str = ""


class FreezeRecord(BaseModel):
    """Result of freezing an evalset (DD-09 §3)."""

    evalset_id: str
    git_ref: str
    content_hash: str
    frozen_at: str


class LineageNode(BaseModel):
    """A node in a SUT's version lineage graph."""

    version_id: str
    label: str = Field(description="tag or branch name")
    kind: Literal["baseline", "candidate"]
    parent_ids: list[str] = Field(default_factory=list)
    experiment_ids: list[str] = Field(default_factory=list)


class LineageGraph(BaseModel):
    """Version lineage for one SUT (baseline tags + candidate branches)."""

    nodes: list[LineageNode] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)
