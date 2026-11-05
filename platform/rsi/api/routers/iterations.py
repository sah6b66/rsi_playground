"""Iteration endpoints incl. four human gates (DD-13 §9.x, §5.3). P0 in-memory."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.core.enums import IterationState
from rsi.core.errors import InvalidTransitionError, NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/iterations", tags=["iterations"])

_TERMINAL = {IterationState.ABORTED, IterationState.FINISHED,
             IterationState.ARCHIVED}

# gate name -> (required paused-step, step after the gate)
_GATES: dict[str, tuple[str, str]] = {
    "confirm-direction": ("awaiting_direction", "attributing"),
    "hypothesis": ("awaiting_hypothesis", "hypothesizing"),
    "review": ("awaiting_review", "reviewing"),
    "decision": ("awaiting_decision", "deciding"),
}


class IterationCreate(BaseModel):
    sut_id: str
    profile_id: str
    mode: str = "auto"


class IterationOut(BaseModel):
    id: str
    sut_id: str
    profile_id: str
    mode: str
    state: str
    current_step: str | None
    round: int
    experiments: list[str]
    budget_used: dict[str, Any]
    archived: bool
    created_at: str


class GateBody(BaseModel):
    """Generic gate payload; concrete fields depend on the gate."""
    model_config = {"extra": "allow"}

    selected: str | None = None
    hypothesis: str | None = None
    approved: bool | None = None
    decision: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, iteration_id: str) -> dict:
    try:
        return store[iteration_id]
    except KeyError:
        raise NotFoundError(
            f"iteration {iteration_id!r} not found",
            context={"iteration_id": iteration_id},
        ) from None


def _out(it: dict) -> IterationOut:
    return IterationOut(
        id=it["id"], sut_id=it["sut_id"], profile_id=it["profile_id"],
        mode=it["mode"], state=it["state"], current_step=it["current_step"],
        round=it["round"], experiments=it["experiments"],
        budget_used=it["budget_used"], archived=it["archived"],
        created_at=it["created_at"],
    )


@router.post("", response_model=None, status_code=201)
@audit.audited("iteration.create", object_type="iteration")
async def create_iteration(
    body: IterationCreate,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("iterations")),
) -> dict:
    it = {
        "id": f"iter-{uuid.uuid4().hex[:8]}",
        "sut_id": body.sut_id,
        "profile_id": body.profile_id,
        "mode": body.mode,
        "state": IterationState.ATTRIBUTING.value,
        "current_step": "awaiting_direction",
        "round": 1,
        "experiments": [],
        "budget_used": {"llm_calls": 0, "tokens": 0},
        "archived": False,
        "paused_from": None,
        "gates": [],
        "created_at": _now(),
    }
    store[it["id"]] = it
    return {"iteration_id": it["id"]}


@router.get("/{iteration_id}", response_model=IterationOut)
async def get_iteration(
    iteration_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("iterations")),
) -> IterationOut:
    return _out(_get(store, iteration_id))


def _transition(it: dict, action: str) -> None:
    if IterationState(it["state"]) in _TERMINAL or it["archived"]:
        raise InvalidTransitionError(
            f"cannot {action} iteration in terminal state {it['state']}",
            context={"iteration_id": it["id"], "from_state": it["state"]},
        )


@router.post("/{iteration_id}:pause", response_model=IterationOut)
@audit.audited("iteration.pause", object_type="iteration")
async def pause_iteration(
    iteration_id: str,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("iterations")),
) -> IterationOut:
    it = _get(store, iteration_id)
    _transition(it, "pause")
    if it["state"] == IterationState.PAUSED.value:
        raise InvalidTransitionError(
            "iteration is already paused",
            context={"iteration_id": iteration_id, "from_state": it["state"]},
        )
    it["paused_from"] = it["state"]
    it["state"] = IterationState.PAUSED.value
    return _out(it)


@router.post("/{iteration_id}:resume", response_model=IterationOut)
@audit.audited("iteration.resume", object_type="iteration")
async def resume_iteration(
    iteration_id: str,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("iterations")),
) -> IterationOut:
    it = _get(store, iteration_id)
    if it["state"] != IterationState.PAUSED.value:
        raise InvalidTransitionError(
            f"cannot resume iteration in state {it['state']}",
            context={"iteration_id": iteration_id, "from_state": it["state"]},
        )
    it["state"] = it["paused_from"] or IterationState.ATTRIBUTING.value
    it["paused_from"] = None
    return _out(it)


@router.post("/{iteration_id}:abort", response_model=IterationOut)
@audit.audited("iteration.abort", object_type="iteration")
async def abort_iteration(
    iteration_id: str,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("iterations")),
) -> IterationOut:
    it = _get(store, iteration_id)
    _transition(it, "abort")
    it["state"] = IterationState.ABORTED.value
    it["paused_from"] = None
    return _out(it)


@router.delete("/{iteration_id}", status_code=204)
@audit.audited("iteration.archive", object_type="iteration")
async def delete_iteration(
    iteration_id: str,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("iterations")),
) -> Response:
    it = _get(store, iteration_id)
    it["archived"] = True
    it["state"] = IterationState.ARCHIVED.value
    it["paused_from"] = None
    return Response(status_code=204)


def _gate_router(name: str, required_step: str, next_step: str):
    @router.post(f"/{{iteration_id}}/{name}", status_code=204)
    @audit.audited(f"gate.{name.replace('-', '_')}", object_type="iteration")
    async def gate(
        iteration_id: str,
        body: GateBody,
        user: Claims = Depends(require_perms("approval:decide")),
        store: dict = Depends(get_store("iterations")),
    ) -> Response:
        it = _get(store, iteration_id)
        if it["state"] != IterationState.PAUSED.value:
            raise InvalidTransitionError(
                f"gate {name!r} requires paused iteration, got {it['state']}",
                context={"iteration_id": iteration_id, "from_state": it["state"]},
            )
        it["state"] = it["paused_from"] or IterationState.ATTRIBUTING.value
        it["paused_from"] = None
        it["current_step"] = next_step
        it["gates"].append(
            {"gate": name, "step": required_step, "by": str(user.sub),
             "at": _now()}
        )
        return Response(status_code=204)

    gate.__name__ = f"gate_{name.replace('-', '_')}"
    return gate


for _name, (_step_in, _step_out) in _GATES.items():
    _gate_router(_name, _step_in, _step_out)
