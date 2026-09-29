-- ===========================================================================
-- 0003_message_media_refs.sql —— 把「这条消息有哪些媒体待取」写进信封
--
-- 背景：Connector 产出的 SourceMessage.media_refs 在 0001 的 message 表里**没有落脚点**
-- （forward_chain 有 JSON 列，媒体引用却没有）。取媒体发生在 L3，而 message 行是 L2
-- 落的；少了这一列，worker 拿不到任何引用，只能回头再问一次平台——对拉取型平台等于
-- 重放整个同步，对推送型平台根本做不到（推送事件不会重来）。
--
-- 语义：平台侧媒体引用的 JSON 数组（file_id / CDN URL / 本地路径），顺序即内容项顺序。
-- 它是**结构化信封**的一部分，与 raw_payload 的三档保留策略无关：无论原始载荷留不留，
-- 这一列都要在，否则消息不可重放（DESIGN §3.2.1）。
--
-- 依据：DESIGN §2.1（L2 落库 / L3 取字节）、§3.2（message 字段清单）、§3.2.1。
-- 执行语义同前：整篇在单个事务内执行，失败整体回滚；本文件不含 PRAGMA。
-- ===========================================================================

ALTER TABLE message ADD COLUMN media_refs TEXT NOT NULL DEFAULT '[]'
    CHECK (json_valid(media_refs));
