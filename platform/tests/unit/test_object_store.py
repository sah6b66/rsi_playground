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
    import sqlite3

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
