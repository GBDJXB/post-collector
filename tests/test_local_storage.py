"""本地存储后端：内容寻址、原子写入、幂等重写。

存储层最容易出的两类事故是"半截文件被当成有效本体"和"同一份内容被写了两遍"。
这组测试钉的就是这两条，外加对象键与磁盘路径的对应关系——**数据库里只存键**，
所以键与路径的映射一旦写错，内容就会"在库里但找不到"。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pc.adapters.local_storage import LocalStorageBackend, LocalStorageError
from pc.domain import ObjectKey

JPEG = b"\xff\xd8\xff\xe0" + b"payload" * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"other" * 4


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(tmp_path / "media")


def _key(data: bytes) -> ObjectKey:
    return ObjectKey(hashlib.sha256(data).hexdigest())


class TestContentAddressing:
    def test_put_returns_content_addressed_key(self, storage: LocalStorageBackend) -> None:
        key = storage.put(JPEG)
        assert key == _key(JPEG)
        assert len(key.sha256) == 64

    def test_object_lands_at_sharded_path(self, storage: LocalStorageBackend) -> None:
        key = storage.put(JPEG)
        expected = storage.root / key.sha256[:2] / key.sha256[2:4] / key.sha256
        assert expected.is_file(), "对象必须落在 ab/cd/<sha256> 上，否则换后端要搬数据"
        assert storage.path_for(key) == expected
        assert expected.read_bytes() == JPEG

    def test_two_objects_shard_independently(self, storage: LocalStorageBackend) -> None:
        storage.put(JPEG)
        storage.put(PNG)
        assert storage.usage()["objects"] == 2
        assert storage.exists(_key(JPEG)) and storage.exists(_key(PNG))

    def test_put_is_idempotent_and_does_not_rewrite(self, storage: LocalStorageBackend) -> None:
        key = storage.put(JPEG)
        before = storage.path_for(key).stat().st_mtime_ns
        again = storage.put(JPEG)
        assert again == key
        assert storage.path_for(again).stat().st_mtime_ns == before, (
            "同内容重复写入不该重写文件（白费一次写盘，还会打乱增量备份）"
        )


class TestAtomicWrite:
    def test_no_temp_file_is_left_behind(self, storage: LocalStorageBackend) -> None:
        storage.put(JPEG)
        leftovers = [p for p in storage.root.rglob("*") if p.name.startswith(".tmp-")]
        assert leftovers == [], f"写完后不该留下临时文件：{leftovers}"

    def test_written_bytes_are_complete(self, storage: LocalStorageBackend) -> None:
        big = b"x" * 200_000
        key = storage.put(big)
        assert storage.path_for(key).stat().st_size == len(big)

    def test_root_being_a_file_is_reported(self, tmp_path: Path) -> None:
        blocked = tmp_path / "media"
        blocked.write_text("not a directory", encoding="utf-8")
        storage = LocalStorageBackend(blocked)
        with pytest.raises(LocalStorageError, match="不是目录"):
            storage.put(JPEG)


class TestReadAndDelete:
    def test_get_roundtrip(self, storage: LocalStorageBackend) -> None:
        key = storage.put(JPEG)
        assert storage.get(key) == JPEG

    def test_get_missing_raises_keyerror(self, storage: LocalStorageBackend) -> None:
        with pytest.raises(KeyError):
            storage.get(_key(b"never stored"))

    def test_exists_is_false_before_put(self, storage: LocalStorageBackend) -> None:
        assert storage.exists(_key(JPEG)) is False

    def test_delete_removes_only_the_object(self, storage: LocalStorageBackend) -> None:
        key = storage.put(JPEG)
        other = storage.put(PNG)
        storage.delete(key)
        assert not storage.exists(key)
        assert storage.exists(other), "删一个对象不该影响另一个"
        storage.delete(key)  # 再删一次是安全的（幂等）

    def test_presign_returns_an_absolute_path(self, storage: LocalStorageBackend) -> None:
        key = storage.put(JPEG)
        signed = storage.presign(key)
        assert Path(signed).is_absolute()
        assert Path(signed) == storage.path_for(key).resolve()


class TestReconciliationHelpers:
    def test_iter_keys_finds_objects_and_skips_junk(self, storage: LocalStorageBackend) -> None:
        first = storage.put(JPEG)
        second = storage.put(PNG)
        # 手工塞进来的垃圾：临时文件、名字不是 64 位十六进制、别处的文件
        (storage.root / ".tmp-123").write_bytes(b"partial")
        (storage.root / "README.txt").write_text("note", encoding="utf-8")
        assert set(storage.iter_keys()) == {first, second}

    def test_iter_keys_on_missing_root_is_empty(self, tmp_path: Path) -> None:
        assert list(LocalStorageBackend(tmp_path / "nope").iter_keys()) == []

    def test_usage_counts_objects_and_bytes(self, storage: LocalStorageBackend) -> None:
        storage.put(JPEG)
        storage.put(PNG)
        usage = storage.usage()
        assert usage["objects"] == 2
        assert usage["bytes"] == len(JPEG) + len(PNG)

    def test_repr_mentions_root(self, storage: LocalStorageBackend) -> None:
        assert "LocalStorageBackend" in repr(storage)


def test_backend_id_is_carried_for_the_database_row(tmp_path: Path) -> None:
    """``storage_backend`` 行的 id 挂在实例上，便于 media 行记录它。"""
    storage = LocalStorageBackend(tmp_path / "media", backend_db_id=7)
    assert storage.backend_db_id == 7


def test_object_key_case_matches_content_addressed_identity() -> None:
    lower = ObjectKey("ab" * 32)
    upper = ObjectKey("AB" * 32)
    assert upper.sha256 == lower.sha256
    assert upper == lower
    assert len({lower, upper}) == 1
    assert upper.value == lower.value == "ab/ab/" + "ab" * 32


def test_object_key_rejects_bad_hash() -> None:
    with pytest.raises(ValueError):
        ObjectKey("not-a-hash")
