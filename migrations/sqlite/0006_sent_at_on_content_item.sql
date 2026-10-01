-- ===========================================================================
-- 0006_sent_at_on_content_item.sql —— 把「消息时间」落到内容项上，让排序能走索引
--
-- 背景（实测，不是猜测）：0005 只给 message(sent_at) 加了单列索引，想让"按消息时间
-- 排序"沿它走。真实查询形状是
--
--     FROM message m JOIN content_item c ON c.message_id = m.message_id
--     ORDER BY m.sent_at DESC, c.content_id DESC LIMIT 50
--
-- 排序的第二个键在**另一张表**上，而 SQLite 只肯用单表索引满足 ORDER BY 的全部键；
-- 跨表混合的排序键永远落到临时 B 树。十万条实测：规划器挑了另一条覆盖索引扫描
-- message_source_sent_idx 再整体排序，P50 354 ms / P99 381 ms —— 越过 §5.6 的
-- P50 < 150 ms，即使 P99 还在 500 ms 以内。
--
-- 因此把 sent_at 复制到 content_item 上，并在同一张表上建 (sent_at DESC, content_id DESC)
-- 的部分索引：同一条查询变成沿索引走 + 主键回表，十万条实测 P50 0.0 ms / P99 0.4 ms。
--
-- 这一列是**派生的**（来自 message.sent_at），遵守 §2.3 的派生数据纪律：
--   * 落库时由 L3 从消息行抄下来；
--   * `pc db reindex --recompute-derived` 会按 message.sent_at 重新对齐（不一样就修）；
--   * `pc doctor` 与 `pc search` 的"该重算了"提示把它算在内。
-- 事实源仍然是 message.sent_at；这一列只服务于"按消息时间过滤与排序"，不作为真相。
-- 消息落库后不再改动（L2 之后没有更新路径），所以两者的偏差只可能来自升级前的老行。
--
-- 0005 建的 message_sent_idx 至此没有查询用它（它是为同一条查询加的），留着的唯一效果
-- 是拖慢 message 的写入并占地方，故一并删除。0005 本身保持原样（已应用的迁移不可改）。
--
-- 依据：DESIGN §3.2（content_item 字段清单）、§5.6（性能预算与实测）、§2.3（派生数据）。
-- 执行语义同前：整篇在单个事务内执行，失败整体回滚；本文件不含 PRAGMA，不重建表。
-- ===========================================================================

ALTER TABLE content_item ADD COLUMN sent_at TEXT;

CREATE INDEX content_item_sent_idx ON content_item (sent_at DESC, content_id DESC)
    WHERE deleted_at IS NULL;

DROP INDEX message_sent_idx;
