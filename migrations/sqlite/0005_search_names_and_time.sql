-- ===========================================================================
-- 0005_search_names_and_time.sql —— 文件名与「消息时间」进入检索
--
-- 两件事，都很小：
--
-- 1) content_item.original_name —— 这条内容进来时的**文件名**（只存 basename）。
--    为什么放在 content_item 而不是 media：media 是按 sha256 内容寻址的，同一份字节
--    重复上传、名字不同时它只有一行；名字属于「这一条是以什么名字进来的」，所以每条
--    内容项各留各的。
--    为什么只存 basename：数据库里不存位置（AGENTS「不把媒体文件路径写进数据库」），
--    而且 DESIGN §6.6 的威胁表要求「文件名不参与路径拼接」——本列只用于展示与检索，
--    任何人都不许拿它去拼路径。截断（255 字符）在写入侧做，不放 CHECK：超长名字应当
--    被截断保存，而不是让整条内容入库失败。
--    注意：它**不是** content_fts 的索引列。检索用的文本由 search_extra 承担（名字、
--    类型、备注都拼进那一列），这样不必重建 external content 虚拟表与 6 个触发器。
--
-- 2) message_sent_idx —— 按「收到消息时间」过滤/排序时的驱动索引。
--    message 上原本只有 message_source_sent_idx (source_id, sent_at DESC)，它要求先定
--    来源；不限定来源又要按 sent_at 排序时，SQLite 只能把 join 结果整体排序（十万行
--    一次排序足以击穿 DESIGN §5.6 的 P99 500 ms）。加上这一条之后，查询可以沿
--    sent_at 顺序驱动 message、再按 message_id 回表取内容项，凑满一页就停。
--    部分索引 WHERE deleted_at IS NULL 与 content_item_time_idx 同理：软删的行不进索引。
--
-- 依据：DESIGN §3.2（content_item 字段清单）、§5.1（检索字段合成）、§5.6（时间排序的
-- 性能预算）、§6.6（文件元数据威胁）。执行语义同前：整篇在单个事务内执行，失败整体
-- 回滚；本文件不含 PRAGMA，也不需要重建表（只加列与索引，故不用 -- pc:foreign_keys=off）。
-- ===========================================================================

ALTER TABLE content_item ADD COLUMN original_name TEXT;

CREATE INDEX message_sent_idx ON message (sent_at DESC) WHERE deleted_at IS NULL;
