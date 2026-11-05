# D2 (Web Input Hints) + D3 (Debug Logging) Implementation Plan

## D2: Web Input Hints
Add `Field(example=...)` / `Field(json_schema_extra={"example": ...})` to all Pydantic request/response models in API routers. Add placeholder text conventions to DD-14.

## D3: Debug Logging Pattern
### Standard pattern for every key function:
```python
from rsi.log import get_logger
logger = get_logger(__name__)

async def some_function(param1: str, param2: int) -> Result:
    logger.debug("some_function.entry", param1=param1, param2=param2)
    ...
    logger.debug("some_function.exit", result_summary=..., duration_ms=...)
    return result
```

### Modules to instrument (all files in each):
1. `rsi/sandbox/` — service.py, backends/fake.py, readiness.py, config_api.py, scheduler.py
2. `rsi/carrier/` — service.py, hosts.py, network.py, scheduler.py, extract.py, overlay.py
3. `rsi/tracing/` — collector.py, store.py, parsers.py
4. `rsi/judge/` — api.py, deterministic.py, safety.py, llm_judge.py, antihack.py, aggregate.py
5. `rsi/decision/` — aggregator.py, selector.py
6. `rsi/optimizer/` — targets.py, attribution.py
7. `rsi/orchestrator/` — batch.py, iteration.py, budget.py, checkpoint.py, event_bus.py
8. `rsi/instrumentor/` — facade.py, scanner.py, planner.py, injector.py, verifier.py
9. `rsi/api/` — app.py, deps.py, streaming.py (router files get logger from deps)

### What to log:
- **Entry**: function name + all parameters (truncate large values to 200 chars)
- **Exit**: return type/summary + duration (where applicable)
- **Branch points**: key decision results (e.g., "cache_hit=True", "retry_round=2")
- **Error paths**: exception type + context before re-raising

### What NOT to log:
- Full credential values (log fingerprint only)
- Full LLM request/response bodies (log hash + length)
- Full trace event payloads (log run_id + event_type + count)

### Implementation approach:
- Batch dispatch per module group (3-4 modules per subagent)
- Each subagent adds logger import + entry/exit debug calls
- Tests: verify debug logging doesn't break existing tests
- Commit after each module group
