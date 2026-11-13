"""F3/B1：对象文件库——store 记录重启持久化（suts/evalsets/iterations 等）."""
import json
from pathlib import Path

from rsi.api.object_store import ObjectFileStore, hydrate_object_stores


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

