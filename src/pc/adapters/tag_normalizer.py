"""标签名称规范化、别名解析与有效关联的 SQLite 条件。"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Sequence

from pc.domain import TagCandidate


def effective_assignment(alias: str = "ta") -> str:
    """只接受未撤销的有效来源和可检索标签，保留历史弃用标签。"""
    return (
        f"{alias}.revoked_at IS NULL AND {alias}.source IN ('human','system','auto','merged')"
        f" AND {alias}.tag_id IN (SELECT tag_id FROM tag WHERE status IN ('active','deprecated'))"
    )


def normalize_tag(name: str) -> str:
    if not isinstance(name, str) or any(unicodedata.category(c) == "Cc" for c in name):
        raise ValueError("标签名称必须是文本，且不含控制字符")
    value = re.sub(r"[\s_]+", "_", unicodedata.normalize("NFKC", name).casefold()).strip("_")
    if not value or len(value) > 255:
        raise ValueError("标签名称规范化后须为 1~255 字符")
    return value


class SqliteTagNormalizer:
    """规范名和别名共享查询入口，重名检查由写服务在事务中执行。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def normalize(self, name: str) -> str:
        return normalize_tag(name)

    def resolve(self, name: str, namespace: str | None = None) -> sqlite3.Row | None:
        norm = self.normalize(name)
        rows = self.conn.execute(
            "SELECT * FROM tag WHERE name_norm=? OR tag_id IN "
            "(SELECT tag_id FROM tag_synonym WHERE alias_norm=?)",
            (norm, norm),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("规范名与别名指向不同标签，请先修正冲突")
        row = rows[0] if rows else None
        if row is not None and namespace is not None and row["namespace"] != namespace:
            raise ValueError(f"标签已属于 {row['namespace']}，不能重复创建到 {namespace}")
        return row

    def expand(self, name: str) -> set[str]:
        row = self.resolve(name)
        if row is None:
            return {self.normalize(name)}
        return {
            row["name_norm"],
            *(
                r[0]
                for r in self.conn.execute(
                    "SELECT alias_norm FROM tag_synonym WHERE tag_id=?", (row["tag_id"],)
                )
            ),
        }

    def resolve_conflicts(self, candidates: Sequence[TagCandidate]) -> list[TagCandidate]:
        """普通标签按规范名去重；同名跨命名空间冲突由服务校验。"""
        unique: dict[tuple[str, str], TagCandidate] = {}
        for candidate in candidates:
            key = (str(candidate.namespace), self.normalize(candidate.tag))
            if key not in unique or candidate.confidence > unique[key].confidence:
                unique[key] = candidate
        return list(unique.values())
