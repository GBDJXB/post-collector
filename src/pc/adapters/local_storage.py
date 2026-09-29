"""本地目录存储后端（DESIGN §3.5、§9.1：``StorageBackend`` 的本地实现）。

对象键是**内容寻址**的 ``ab/cd/<sha256>``，因此：

* 同一份内容重复入库是幂等的（文件已存在就不重写，省一次写盘）；
* 从本地目录换到 NAS / S3 时对象键不变，不需要数据搬迁；
* 目录天然分片（每层 256 个桶），单目录不会堆积几十万个文件。

刻意不做的事：

* **不往对象旁边写元数据**。类型、尺寸、扫描结论都在数据库里；对象目录只有字节，
  这样 rsync / 对账脚本看到的是一棵纯粹的、可用哈希自证的文件树。
* **不负责删元数据**。删对象与删元数据是两件事，谁先谁后由一致性任务对账（DESIGN §7.1）。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

from pc.domain import ObjectKey

#: 临时文件前缀。写在**同一个目录**里，保证 ``os.replace`` 是同文件系统的原子改名。
_TEMP_PREFIX = ".tmp-"


class LocalStorageError(RuntimeError):
    """本地存储后端不可用（目录不可写、目标不是文件等）。"""


class LocalStorageBackend:
    """把媒体本体存进一个本地目录（默认 ``<data_root>/media``）。

    实例本身**无状态**（只有 root 一个字段），可以每个进程/每次任务新建；数据库里的
    ``storage_backend`` 行通过 ``backend_db_id`` 关联，方便将来一个库挂多个后端。
    """

    def __init__(self, root: Path | str, *, backend_db_id: int | None = None) -> None:
        self._root = Path(root).expanduser()
        self.backend_db_id = backend_db_id

    @property
    def root(self) -> Path:
        return self._root

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"LocalStorageBackend(root={self._root!r}, backend_db_id={self.backend_db_id})"

    # -- 路径 ---------------------------------------------------------------

    def path_for(self, key: ObjectKey) -> Path:
        """对象键 → 磁盘路径。**不要**把这个路径写进数据库。"""
        return self._root.joinpath(*key.value.split("/"))

    def _require_root(self) -> None:
        if self._root.exists() and not self._root.is_dir():
            raise LocalStorageError(f"存储根不是目录：{self._root}")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LocalStorageError(f"存储根不可写：{self._root}\n  原始错误：{exc}") from exc

    # -- StorageBackend 契约 ------------------------------------------------

    def put(self, data: bytes, *, content_type: str | None = None) -> ObjectKey:
        """写入字节并返回内容寻址键；同内容重复写入幂等（不重写、不改 mtime）。

        ``content_type`` 刻意不落盘：类型是数据库里的元数据，由扫描器判定
        （以魔数为准，见 ``pc.adapters.magic_scan``），不由存储层记录。
        """
        del content_type
        key = ObjectKey(hashlib.sha256(data).hexdigest())
        target = self.path_for(key)
        if target.is_file():
            return key

        self._require_root()
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子改名：半截文件永远不会被当成有效本体
        temporary = target.parent / f"{_TEMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise LocalStorageError(f"写入媒体失败：{target}\n  原始错误：{exc}") from exc
        return key

    def get(self, key: ObjectKey) -> bytes:
        """按键读取；不存在时抛 ``KeyError``（契约如此，不是 ``FileNotFoundError``）。"""
        path = self.path_for(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise KeyError(f"对象不存在：{key}") from exc

    def exists(self, key: ObjectKey) -> bool:
        return self.path_for(key).is_file()

    def delete(self, key: ObjectKey) -> None:
        """删除本体。**删元数据不删本体**，反之亦然，由一致性任务对账（DESIGN §7.1）。"""
        self.path_for(key).unlink(missing_ok=True)

    def presign(self, key: ObjectKey, *, expires_in: int = 300) -> str:
        """本地目录没有签名 URL 可言，返回绝对路径（接口留出 S3 的位置）。

        ``expires_in`` 被忽略：本地路径不会过期。调用方**不得**把返回值持久化——
        一旦后端换成 S3，它就变成了一张会过期的临时链接。
        """
        del expires_in
        return str(self.path_for(key).resolve())

    # -- 运维辅助（不是端口契约的一部分，给对账与容量统计用） ----------------

    def iter_keys(self) -> Iterator[ObjectKey]:
        """遍历根目录下所有合法对象键，供一致性对账（§7.1）使用。

        只看两级分片下的 64 位十六进制文件名；临时文件、别的东西一律忽略——
        对账脚本不该因为清理残留的临时文件而误报。
        """
        if not self._root.is_dir():
            return
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.name.startswith(_TEMP_PREFIX):
                continue
            try:
                key = ObjectKey(path.name)
            except ValueError:
                continue
            if self.path_for(key) == path:
                yield key

    def usage(self) -> dict[str, int]:
        """对象数与占用字节（按实际落盘文件统计）。"""
        count = 0
        total = 0
        for key in self.iter_keys():
            count += 1
            total += self.path_for(key).stat().st_size
        return {"objects": count, "bytes": total}


__all__ = ["LocalStorageBackend", "LocalStorageError"]
