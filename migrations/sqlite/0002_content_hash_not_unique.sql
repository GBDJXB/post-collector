-- ===========================================================================
-- 0002_content_hash_not_unique.sql —— 文本的精确重复是「归组」，不是「禁止存在」
--
-- 背景：0001 给 content_item.content_hash 建了**唯一**部分索引。但
-- content_item.message_id 是 NOT NULL（一个内容项必属于某条消息），所以
-- 「同一段文本出现在两条消息里」在唯一索引下**根本无法表示**——第二条会被直接
-- 拒绝。这与 v3.1 修掉的 media 多对多缺陷是同一类问题。
--
-- 处置：把该索引降级为普通部分索引，重复的表示交给 3.6/3.8 已经设计好的
-- canonical 归组（dup_group_id + canonical_id，导入层负责填写）。
--
-- 刻意**不动**的东西：media.sha256 仍然是全局唯一——字节层面的"同一份内容只有
-- 一行 media"是硬约束，它保证了不会重复占盘。
--
-- 依据：DESIGN §3.2（索引清单）、§3.6（去重与 canonical 归并）、§3.8（可测试语义）。
-- 执行语义同 0001：整篇在单个事务内执行，失败整体回滚；本文件不含 PRAGMA。
-- ===========================================================================

DROP INDEX content_item_content_hash_key;

CREATE INDEX content_item_content_hash_idx ON content_item (content_hash)
    WHERE content_hash IS NOT NULL AND deleted_at IS NULL;
