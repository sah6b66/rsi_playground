"""Iteration endpoints incl. four human gates (DD-13 §9.x, §5.3). P0 in-memory."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.core.enums import IterationState
from rsi.core.errors import (
    ConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
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

# After a gate passes, the iteration pauses next at the following gate's
# awaiting step (D-3: current_step must match the gate actually reached).
_GATE_NEXT_STEP: dict[str, str] = {
    "confirm-direction": "awaiting_hypothesis",
    "hypothesis": "awaiting_review",
    "review": "awaiting_decision",
    "decision": "deciding",
}


class IterationCreate(BaseModel):
    """§9.12：``mode ∈ p1|p2``；p2 在 P2 阶段开放（此前 409）。"""

    sut_id: str = Field(json_schema_extra={"example": "sut-1a2b3c4d"})
    profile_id: str = Field(json_schema_extra={"example": "prof-9f8e7d6c"})
    mode: str = Field(default="p1", json_schema_extra={"example": "p1"})


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

    selected: str | None = Field(
        default=None, json_schema_extra={"example": "exp-11223344"}
    )
    hypothesis: str | None = Field(
        default=None,
        json_schema_extra={
            "example": "Adding explicit verification steps to the system prompt will reduce missed checks"
        },
    )
    approved: bool | None = Field(default=None, json_schema_extra={"example": True})
    decision: str | None = Field(
        default=None, json_schema_extra={"example": "apply_exp_exp-11223344"}
    )


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
    suts: dict = Depends(get_store("suts")),
) -> dict:
    # mode 校验（§9.12 / T2-7）：p2 未开放 → 409；非法值 → 422
    if body.mode == "p2":
        raise ConflictError(
            "P2 未开放：全自动迭代模式于 P2 阶段开放",
            context={"mode": body.mode},
        )
    if body.mode != "p1":
        raise ValidationError(
            f"invalid mode {body.mode!r}: expected 'p1'",
            context={"mode": body.mode},
        )
    if body.sut_id not in suts:
        raise NotFoundError(
            f"sut {body.sut_id!r} not found", context={"sut_id": body.sut_id}
        )
    # v2.14 迭代互斥：同一 sut_id 存在活跃迭代 → 409
    for other in store.values():
        if (
            other["sut_id"] == body.sut_id
            and IterationState(other["state"]) not in _TERMINAL
            and not other["archived"]
        ):
            raise ConflictError(
                f"sut {body.sut_id!r} already has an active iteration "
                f"{other['id']!r}",
                context={"sut_id": body.sut_id, "iteration_id": other["id"]},
            )
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
    # D-3: pausing moves the iteration to its next human gate; current_step
    # must reflect which gate the iteration is actually waiting at.
    order = list(_GATES)
    passed = sum(1 for g in it["gates"])
    it["current_step"] = (
        _GATES[order[passed]][0] if passed < len(order) else "deciding"
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


async def _engine_gate(engine, name: str, body: GateBody) -> str | None:
    """Forward a gate submission to a live engine instance (D-3).

    The engine validates state + current_step itself and raises
    InvalidTransitionError (409) on mismatch. Returns the hypothesis id for
    gate 2 when the engine produced one.
    """
    if name == "confirm-direction":
        selected = body.selected
        if selected is None and body.model_extra:
            selected = body.model_extra.get("selected")
        selected_list = [selected] if isinstance(selected, str) else list(
            selected or []
        )
        await engine.gate_confirm_direction(selected_list)
        return None
    if name == "hypothesis":
        hyp = {"summary": body.hypothesis or ""}
        if body.model_extra:
            for key in ("target", "predicted_effect"):
                if key in body.model_extra:
                    hyp[key] = body.model_extra[key]
        return await engine.gate_hypothesis(hyp)
    if name == "review":
        verdict = "pass" if body.approved is not False else "reject"
        return await engine.gate_review(verdict, [])
    if name == "decision":
        return await engine.gate_decision(body.decision or "promote")
    raise ValidationError(
        f"unknown gate {name!r}", context={"gate": name}
    )


def _gate_router(name: str, required_step: str, next_step: str):
    status_code = 201 if name == "hypothesis" else 204

    @router.post(f"/{{iteration_id}}/{name}", status_code=status_code)
    @audit.audited(f"gate.{name.replace('-', '_')}", object_type="iteration")
    async def gate(
        iteration_id: str,
        body: GateBody,
        user: Claims = Depends(require_perms("approval:decide")),
        store: dict = Depends(get_store("iterations")),
        hypotheses: dict = Depends(get_store("hypotheses")),
    ) -> Response:
        from rsi.orchestrator.iteration import RUNNING_ENGINES

        it = _get(store, iteration_id)
        if it["state"] != IterationState.PAUSED.value:
            raise InvalidTransitionError(
                f"gate {name!r} requires paused iteration, got {it['state']}",
                context={"iteration_id": iteration_id, "from_state": it["state"]},
            )
        # D-3: the gate must match the step the iteration is actually paused
        # at — the wrong gate is a 409 even when the state is PAUSED.
        if it["current_step"] != required_step:
            raise InvalidTransitionError(
                f"gate {name!r} expects current_step {required_step!r}, "
                f"got {it['current_step']!r}",
                context={
                    "iteration_id": iteration_id,
                    "current_step": it["current_step"],
                    "required_step": required_step,
                },
            )
        engine_hyp_id: str | None = None
        engine = RUNNING_ENGINES.get(iteration_id)
        if engine is not None:
            # D-3 bridge: a live engine instance validates state + step
            # itself (InvalidTransitionError -> 409 on mismatch).
            engine_hyp_id = await _engine_gate(engine, name, body)
        # no engine (iteration not running in-process): store-only fallback
        it["state"] = it["paused_from"] or IterationState.ATTRIBUTING.value
        it["paused_from"] = None
        it["current_step"] = _GATE_NEXT_STEP.get(name, next_step)
        it["gates"].append(
            {"gate": name, "step": required_step, "by": str(user.sub),
             "at": _now()}
        )
        if name == "hypothesis":
            # 人工门②（§9.12）：写 hypotheses.iteration_id 并推进状态机，
            # 响应 201 + {"hypothesis_id": ...}
            hyp = {
                "id": engine_hyp_id or f"hyp-{uuid.uuid4().hex[:8]}",
                "sut_id": it["sut_id"],
                "iteration_id": it["id"],
                "statement": body.hypothesis or "",
                "target": body.model_extra.get("target") if body.model_extra else None,
                "predicted_effect": (
                    body.model_extra.get("predicted_effect")
                    if body.model_extra
                    else None
                ),
                "status": "submitted",
                "created_at": _now(),
                "updated_at": _now(),
            }
            hypotheses[hyp["id"]] = hyp
            return Response(
                status_code=201,
                content=json.dumps({"hypothesis_id": hyp["id"]}),
                media_type="application/json",
            )
        return Response(status_code=204)

    gate.__name__ = f"gate_{name.replace('-', '_')}"
    return gate


for _name, (_step_in, _step_out) in _GATES.items():
    _gate_router(_name, _step_in, _step_out)
