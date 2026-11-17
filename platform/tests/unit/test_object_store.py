"""F3/B1：对象文件库——store 记录重启持久化（suts/evalsets/iterations 等）."""
import json
from pathlib import Path

from rsi.api.object_store import (
    ObjectFileStore,
    delete_record,
    hydrate_object_stores,
    persist_record,
)


def test_save_load_delete_roundtrip(tmp_path: Path):
    st = ObjectFileStore(tmp_path)
    rec = {"id": "sut-1", "name": "opencode-netops", "carrier": "opencode"}
    st.save("suts", rec)
    f = tmp_path / "suts" / "sut-1.json"
    assert f.is_file()
    assert st.load_all("suts") == [rec]

    st.save("suts", {**rec, "name": "renamed"})
    assert st.load_all("suts")[0]["name"] == "renamed"

    st.delete("suts", "sut-1")
    assert st.load_all("suts") == []
    st.delete("suts", "sut-1")  # 幂等


def test_load_all_skips_corrupt_files(tmp_path: Path):
    (tmp_path / "evalsets").mkdir(parents=True)
    (tmp_path / "evalsets" / "good.json").write_text(
        json.dumps({"id": "good"}), encoding="utf-8")
    (tmp_path / "evalsets" / "bad.json").write_text("{not json", encoding="utf-8")
    st = ObjectFileStore(tmp_path)
    assert st.load_all("evalsets") == [{"id": "good"}]


def test_hydrate_object_stores_merges_into_empty_stores(tmp_path: Path):
    st = ObjectFileStore(tmp_path)
    st.save("suts", {"id": "sut-9", "name": "n"})
    st.save("iterations", {"id": "iter-9", "state": "finished"})

    stores: dict = {"suts": {}, "evalsets": {}, "iterations": {}}
    hydrate_object_stores(stores, tmp_path)
    assert stores["suts"]["sut-9"]["name"] == "n"
    assert stores["iterations"]["iter-9"]["state"] == "finished"
    assert stores["evalsets"] == {}


def test_hydrate_does_not_overwrite_memory(tmp_path: Path):
    st = ObjectFileStore(tmp_path)
    st.save("suts", {"id": "sut-1", "name": "disk"})
    stores: dict = {"suts": {"sut-1": {"id": "sut-1", "name": "memory"}}}
    hydrate_object_stores(stores, tmp_path)
    assert stores["suts"]["sut-1"]["name"] == "memory"



def _sf(tmp_path):
    from sqlmodel import SQLModel, Session, create_engine

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


def test_persist_record_upserts_sut_db_row(tmp_path):
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    rec = {"id": "sut-1", "name": "n1", "carrier": "opencode",
           "image_ref": "img:1", "description": "", "created_at": "2026-01-01"}
    persist_record(services, "suts", rec)
    from sqlmodel import select

    from rsi.db.tables import Sut

    with sf() as s:
        assert s.exec(select(Sut)).one().name == "n1"
    rec["name"] = "n2"
    persist_record(services, "suts", rec)  # upsert 不重复插行
    with sf() as s:
        rows = s.exec(select(Sut)).all()
        assert len(rows) == 1 and rows[0].name == "n2"


def test_persist_record_upserts_evalset_db_row(tmp_path):
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    rec = {"id": "es-1", "name": "e1", "purpose": "iterate",
           "created_at": "2026-01-01"}
    persist_record(services, "evalsets", rec)
    from sqlmodel import select

    from rsi.db.tables import EvalSet

    with sf() as s:
        assert s.exec(select(EvalSet)).one().purpose == "iterate"


def test_persist_record_degrades_without_session_factory(tmp_path):
    st = ObjectFileStore(tmp_path)
    services = {"object_store": st}
    persist_record(services, "suts", {"id": "sut-1", "name": "n",
                                      "carrier": "opencode", "image_ref": "i",
                                      "description": "", "created_at": "x"})
    assert st.load_all("suts")[0]["name"] == "n"  # JSON 落盘不受影响


def test_persist_record_sut_carrier_whitelist_collapse(tmp_path):
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    persist_record(services, "suts", {
        "id": "sut-x", "name": "nx", "carrier": "claude-code",
        "image_ref": "", "description": "", "created_at": "2026-01-01",
    })
    from sqlmodel import select

    from rsi.db.tables import Sut

    with sf() as s:
        assert s.exec(select(Sut)).one().carrier == "custom"
    # JSON 落盘保留 payload 原始值，仅 DB 存在性行收敛
    assert st.load_all("suts")[0]["carrier"] == "claude-code"


def test_delete_record_removes_db_row(tmp_path):
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    rec = {"id": "es-1", "name": "e1", "purpose": "iterate",
           "created_at": "2026-01-01"}
    persist_record(services, "evalsets", rec)
    delete_record(services, "evalsets", "es-1")
    from sqlmodel import select

    from rsi.db.tables import EvalSet

    with sf() as s:
        assert s.exec(select(EvalSet)).all() == []


def test_persist_hypothesis_row_closes_opt_history_loop(tmp_path):
    """B2-T5 修复：hypotheses 行同步闭环 FR6.2——persist_record 写行后
    bootstrap._opt_history_lookup 能回读 failure_type/proposal_type/target/
    status 映射（生产归因"已试过 X 未果"输入不再恒空）。"""
    st = ObjectFileStore(tmp_path)
    sf = _sf(tmp_path)
    services = {"object_store": st, "session_factory": sf}
    # 与 iterations.py 人工门②落 store 的记录形态一致（B2-T5 全 schema 路径）
    rec = {
        "id": "hyp-1",
        "sut_id": "sut-1",
        "iteration_id": "iter-1",
        "statement": "调整 sys.md 补齐漏查的接口状态",
        "target": "suts/sut-1/sys.md",
        "predicted_effect": "task_complete +0.1",
        "status": "submitted",  # API store 域状态，DB 侧须收敛 proposed
        "created_at": "2026-11-16T00:00:00+00:00",
        "hypothesis_json": {  # HypothesisDraftOut 九字段（无 target）
            "problem_trace_ids": ["run-1"],
            "failure_type": "missed_critical_command",
            "defect_class": "A",
            "proposal_type": "modify_prompt",
            "diff_content": "--- a/sys.md\n+++ b/sys.md",
            "expected_improve": "+0.1",
            "risk": "",
            "verification_task_ids": [],
            "predicted_affected_tasks": [],
        },
    }
    persist_record(services, "hypotheses", rec)

    from rsi.bootstrap import _opt_history_lookup

    hist = _opt_history_lookup(sf, "sut-1")
    assert hist == [{
        "failure_mode": "missed_critical_command",
        "decision": "proposed",  # submitted -> ck_hypotheses_status 域
        "proposal_type": "modify_prompt",
        "target": "suts/sut-1/sys.md",  # 顶层 target 合并进 payload
    }]

    # 更新分支：同 id 二次 persist 刷 hypothesis_json/status，不重复插行
    rec["status"] = "rejected"  # 已在 DB 约束域，直通
    rec["hypothesis_json"]["failure_type"] = "call_order_error"
    persist_record(services, "hypotheses", rec)
    hist2 = _opt_history_lookup(sf, "sut-1")
    assert len(hist2) == 1
    assert hist2[0]["decision"] == "rejected"
    assert hist2[0]["failure_mode"] == "call_order_error"
    assert hist2[0]["target"] == "suts/sut-1/sys.md"

    # 法外状态收敛落 draft（不触发 IntegrityError 静默断链）
    rec["status"] = "submitted"
    rec["iteration_id"] = None
    persist_record(services, "hypotheses", rec)
    assert _opt_history_lookup(sf, "sut-1")[0]["decision"] == "proposed"
