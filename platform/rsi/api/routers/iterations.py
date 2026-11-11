"""Iteration endpoints incl. four human gates (DD-13 §9.x, §5.3). P0 in-memory."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from rsi.api.db_reads import db_verdict, list_runs_for_batch
from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
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
    """§9.12：``mode ∈ p1|p2``；p2 在 P2 阶段开放（此前 409）。

    T3 web 向导形态：可携带 ``evalset_id`` 与内嵌 ``profile``（goals/guards/
    budget）；此时 ``profile_id`` 可省略，由 API 自动铸号。
    """

    sut_id: str = Field(json_schema_extra={"example": "sut-1a2b3c4d"})
    profile_id: str | None = Field(
        default=None, json_schema_extra={"example": "prof-9f8e7d6c"}
    )
    mode: str = Field(default="p1", json_schema_extra={"example": "p1"})
    evalset_id: str | None = Field(
        default=None, json_schema_extra={"example": "eval-0001"}
    )
    profile: dict[str, Any] | None = None
    # U7a：人可读会话标识（1-64 字符，中文可用；空 → 列表回退显示 ID 前 8 位）
    label: str | None = Field(
        default=None, min_length=1, max_length=64,
        json_schema_extra={"example": "BGP排障优化-第一轮"},
    )
    # ADR-27 D4：环境准备模式。cli（默认）走 SandboxService lab 管道；
    # api 走系统级 Network Prep API（须给 api_evalset_name 在线评测集）
    prep_mode: str = Field(default="cli", json_schema_extra={"example": "api"})
    api_evalset_name: str | None = Field(
        default=None, json_schema_extra={"example": "lab-bgp-weekly"}
    )


class IterationOut(BaseModel):
    id: str
    sut_id: str
    profile_id: str
    mode: str
    state: str
    label: str | None = None
    current_step: str | None
    round: int
    experiments: list[str]
    budget_used: dict[str, Any]
    archived: bool
    created_at: str
    evalset_id: str | None = None
    profile: dict[str, Any] | None = None
    prep_mode: str = "cli"
    api_evalset_name: str | None = None


class IterationPage(BaseModel):
    items: list[IterationOut]
    next_cursor: str | None = None
    has_more: bool = False


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
        evalset_id=it.get("evalset_id"), profile=it.get("profile"),
        label=it.get("label"),
        prep_mode=it.get("prep_mode", "cli"),
        api_evalset_name=it.get("api_evalset_name"),
    )


@router.post("", response_model=None, status_code=201)
@audit.audited("iteration.create", object_type="iteration")
async def create_iteration(
    body: IterationCreate,
    user: Claims = Depends(require_perms("iteration:manage")),
    store: dict = Depends(get_store("iterations")),
    suts: dict = Depends(get_store("suts")),
) -> dict:
    # mode 校验（§9.12 / ADR-21 / T4）：p0 人工模式与 p1 均为一等模式；
    # p2 未开放 → 409；其他值 → 422
    if body.mode == "p2":
        raise ConflictError(
            "P2 未开放：全自动迭代模式于 P2 阶段开放",
            context={"mode": body.mode},
        )
    if body.mode not in ("p0", "p1"):
        raise ValidationError(
            f"invalid mode {body.mode!r}: expected 'p0' or 'p1'",
            context={"mode": body.mode},
        )
    if body.sut_id not in suts:
        raise NotFoundError(
            f"sut {body.sut_id!r} not found", context={"sut_id": body.sut_id}
        )
    # ADR-27 D4: prep_mode 校验 —— api 模式必须指名在线评测集
    if body.prep_mode not in ("cli", "api"):
        raise ValidationError(
            f"invalid prep_mode {body.prep_mode!r}: expected 'cli' or 'api'",
            context={"prep_mode": body.prep_mode},
        )
    if body.prep_mode == "api" and not (body.api_evalset_name or "").strip():
        raise ValidationError(
            "prep_mode='api' requires api_evalset_name",
            context={"prep_mode": body.prep_mode},
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
        "label": body.label,
        "sut_id": body.sut_id,
        "profile_id": body.profile_id or f"prof-{uuid.uuid4().hex[:8]}",
        "evalset_id": body.evalset_id,
        "profile": body.profile,
        "prep_mode": body.prep_mode,
        "api_evalset_name": body.api_evalset_name,
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


@router.get("", response_model=IterationPage)
async def list_iterations(
    response: Response,
    status: str | None = None,
    sut_id: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("iterations")),
) -> IterationPage:
    """会话列表（T3 web）。``status`` 匹配 IterationState 值；``sut_id`` 过滤。"""
    items = list(store.values())
    if status is not None:
        items = [it for it in items if it["state"] == status]
    if sut_id is not None:
        items = [it for it in items if it["sut_id"] == sut_id]
    # 最新创建在前（会话列表惯例）
    items.sort(key=lambda it: it["created_at"], reverse=True)
    window, page = paginate(items, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return IterationPage(
        items=[_out(it) for it in window],
        next_cursor=page["next_cursor"],
        has_more=page["has_more"],
    )


@router.get("/{iteration_id}", response_model=IterationOut)
async def get_iteration(
    iteration_id: str,
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("iterations")),
    batches: dict = Depends(get_store("batches")),
    runs: dict = Depends(get_store("runs")),
) -> IterationOut:
    it = _get(store, iteration_id)
    # T4：budget_used.tokens 尚未累计（0）时，由该迭代关联 runs 兜底聚合
    if not it["budget_used"].get("tokens"):
        out = _out(it)
        out.budget_used = {**it["budget_used"],
                           "tokens": _tokens_used(request, batches, runs,
                                                  iteration_id)}
        return out
    return _out(it)


def _tokens_used(request: Request, batches: dict, runs: dict,
                 iteration_id: str) -> int:
    """Sum run tokens across all batches bound to the iteration."""
    total = 0
    for batch in batches.values():
        if batch.get("iteration_id") != iteration_id:
            continue
        for run in list_runs_for_batch(request, runs, batch["id"]):
            result = run.get("result")
            tokens = (
                result.get("tokens")
                if isinstance(result, dict)
                else run.get("tokens")
            )
            if isinstance(tokens, (int, float)):
                total += int(tokens)
    return total


def _normalize_files(files: Any) -> list[dict]:
    """files_changed entries → [{path, target}]（str 项 target=None）。"""
    out: list[dict] = []
    for f in files or []:
        if isinstance(f, dict):
            out.append({"path": f.get("path"), "target": f.get("target")})
        else:
            out.append({"path": f, "target": None})
    return out


@router.get("/{iteration_id}/timeline")
async def get_iteration_timeline(
    iteration_id: str,
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("iterations")),
    batches: dict = Depends(get_store("batches")),
    runs: dict = Depends(get_store("runs")),
    experiments: dict = Depends(get_store("experiments")),
) -> dict:
    """会话时间线（T4 web / DD-14 §5.4 进展+效果图数据源）。

    内存 stores 聚合（DB 回退走 db_reads）；缺失字段 None/[] —— 诚实空。
    """
    it = _get(store, iteration_id)

    # 本迭代的 batches 按 round 分组（round 缺失按出现顺序编号）
    own = sorted(
        (b for b in batches.values()
         if b.get("iteration_id") == iteration_id),
        key=lambda b: (b.get("round") or 0, b.get("created_at", "")),
    )
    groups: list[tuple[int, list[dict]]] = []
    index_of: dict[int, int] = {}
    for pos, batch in enumerate(own, start=1):
        rnd = batch.get("round")
        if rnd is None:
            rnd = pos
        if rnd not in index_of:
            index_of[rnd] = len(groups)
            groups.append((rnd, []))
        groups[index_of[rnd]][1].append(batch)

    # 本迭代的 experiments（按 id 引用或 iteration_id 归属）
    exp_ids = set(it.get("experiments") or [])
    own_exps = [
        e for e in experiments.values()
        if e["id"] in exp_ids or e.get("iteration_id") == iteration_id
    ]

    rounds = []
    for pos, (rnd, batch_list) in enumerate(groups):
        tasks: list[dict] = []
        judged = passed = 0
        tokens_sum: list[int] = []
        latencies: list[int] = []
        for batch in batch_list:
            for run in list_runs_for_batch(request, runs, batch["id"]):
                verdict = run.get("verdict") or db_verdict(
                    request, run["id"]
                ) or {}
                result = run.get("result")
                result = result if isinstance(result, dict) else {}
                tokens = result.get("tokens", run.get("tokens"))
                latency = result.get("latency_ms", run.get("latency_ms"))
                rcm = verdict.get("root_cause_match")
                if rcm is None:
                    rcm = (verdict.get("dimensions") or {}).get(
                        "root_cause_match"
                    )
                tasks.append({
                    "task_id": run.get("task_id"),
                    "batch_id": batch["id"],
                    "run_id": run["id"],
                    "status": run.get("status"),
                    "tokens": tokens if isinstance(tokens, int) else None,
                    "latency_ms": (
                        latency if isinstance(latency, (int, float))
                        else None
                    ),
                    "root_cause_match": rcm,
                })
                if isinstance(tokens, int):
                    tokens_sum.append(tokens)
                if isinstance(latency, (int, float)):
                    latencies.append(int(latency))
                if verdict.get("verdict") in ("pass", "reject"):
                    judged += 1
                    passed += verdict["verdict"] == "pass"
        exp = own_exps[pos] if pos < len(own_exps) else None
        versions = (exp or {}).get("versions") or {}
        band = versions.get("b", {}).get("noise_band") or [None, None]
        rounds.append({
            "round": rnd,
            "status": batch_list[-1].get("status"),
            "batch_ids": [b["id"] for b in batch_list],
            "config_version_before": versions.get("a", {}).get("version"),
            "config_version_after": versions.get("b", {}).get("version"),
            "files_changed": _normalize_files(
                (exp or {}).get("files_changed")
            ),
            "tasks": tasks,
            "scores": {
                "accuracy": (passed / judged) if judged else None,
                "noise_band": list(band),
            },
        })

    summary = {
        "accuracy_series": [
            {"round": r["round"], "value": r["scores"]["accuracy"]}
            for r in rounds if r["scores"]["accuracy"] is not None
        ],
        "tokens_series": [
            {"round": r["round"], "value": sum(
                t["tokens"] or 0 for t in r["tasks"])}
            for r in rounds
        ],
        "latency_series": [
            {"round": r["round"], "value": (
                sum(t["latency_ms"] or 0 for t in r["tasks"]) / len(r["tasks"])
                if r["tasks"] else None
            )}
            for r in rounds
        ],
    }
    return {
        "iteration_id": iteration_id,
        "label": it.get("label"),
        "rounds": rounds,
        "summary": summary,
    }


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
    user: Claims = Depends(require_perms("iteration:manage")),
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
    user: Claims = Depends(require_perms("iteration:manage")),
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
    user: Claims = Depends(require_perms("iteration:manage")),
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
    user: Claims = Depends(require_perms("iteration:manage")),
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
        # a) explicit approval required: a missing ``approved`` (None) is a
        # 422, not a silent pass — the review gate must never default-open.
        if body.approved is None:
            raise ValidationError(
                "review gate requires an explicit approved (true/false)",
                context={"gate": name, "approved": None},
            )
        verdict = "pass" if body.approved else "reject"
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
        # a) the review gate requires an explicit approved decision — None
        # is a 422 in BOTH modes (engine bridge and store-only fallback),
        # never a default pass.
        if name == "review" and body.approved is None:
            raise ValidationError(
                "review gate requires an explicit approved (true/false)",
                context={"gate": name, "approved": None},
            )
        # D-3 bridge (id-space note, R1#5): this endpoint keys the registry by
        # the API-minted ``iter-<uuid8>`` id. The API itself never starts an
        # IterationEngine — a store-only iteration therefore finds no engine
        # here and falls through to the store-only state-machine update below
        # (no engine validation happens in that mode). A live engine reaches
        # this bridge only when its driver passed the API iteration id into
        # ``iterate_once(iteration_id=...)``, which registers the engine under
        # that same id. A 409 from the engine means the engine's own state
        # machine rejected the gate (mismatched step/state).
        engine = RUNNING_ENGINES.get(iteration_id)
        if engine is not None:
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
