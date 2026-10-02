-- ===========================================================================
-- 0007_post_table.sql —— 派生一张「帖子」表，让检索的结果单位从内容项变成消息
--
-- 背景（DESIGN §4.4 早就这么要求，只是一直没落地）：一条图文混合的消息会落成多个
-- content_item（text[ordinal 0] + image[1] + …），而检索层是"一条内容项 = 一条结果"，
-- 于是同一条消息在结果里散成多行、看不出是一件事。更深的问题是去重按**文本哈希**
-- 折叠：同一句评论配不同图时，后来那条评论会被隐藏，图和评论的配对被打散。
--
-- 这张表是**派生数据**（DESIGN §2.3）：一帖一行，主键就是 message_id，任何时候都能
-- 从 message + content_item + media 重建（`pc db reindex --recompute-derived` 负责修复）。
-- 它只放**稳定的构成摘要**，可见性（哪些部件算"看得见"）永远在查询时实时判定，不入表：
-- 一旦把"可见部件数/NSFW 峰值"这类聚合落下来，状态一变就漂移，而漂移的聚合会静默
-- 影响结果。构成摘要只在部件增删/软删时变化，重算能对齐。
--
-- 字段口径：
--   part_count        未软删部件数（不做可见性过滤）
--   type_mask         部件类型的位图，位序 = pc.domain.ITEM_TYPES 的顺序
--                     （text=1, image=2, video=4, audio=8, link=16, sticker=32, file=64）；
--                     单比特 = 该类型的"纯内容"，多比特 = 跨类型内容（即 mixed）
--   first_seen_at     部件里最早的 first_seen_at（入库视角的帖时间）
--   sent_at           message.sent_at 的副本（消息时间视角的帖时间）
--   content_signature 整帖去重签名，**由 `--recompute-derived` 计算，本文件留空**：
--                     签名要按摘要的**字典序**拼接（同一帖里部件的到达顺序不该影响
--                     签名），而 SQLite 的 group_concat 不保证输出顺序（本机实测：
--                     按扫描顺序输出），也不接受 DISTINCT + 分隔符两个参数。把顺序
--                     依赖留在数据里迟早出错，所以签名的唯一实现放在 Python
--                     （`pc.domain.post_signature`：去重 + 排序 + 拼接 + 超长置空）。
--                     **签名缺失 = 不折叠**：宁可多显示一条，也绝不误折叠两份内容。
--                     签名补齐之前，帖级检索、分页、类型过滤都已经可用（它们只依赖
--                     上面的构成摘要），只有"同内容折叠"要等一次重算。
--   canonical_post_id / dup_group_id  同签名帖里 message_id 最小的那个（帖级折叠用）
--
-- 为什么回填放在迁移里：先建表不回填 = 升级之后**检索返回空**，这是最糟的失败模式。
-- 回填是纯派生数据的确定性计算，且和建表在同一个事务里，失败整体回滚。
--
-- 依据：DESIGN §4.4（混合消息按消息聚合展示）、§3.2（表设计）、§3.6/§3.8（去重语义）、
-- §5.1（检索结果单位）。执行语义同前：整篇在单个事务内执行，本文件不含 PRAGMA，不重建表。
-- ===========================================================================

CREATE TABLE post (
    message_id INTEGER PRIMARY KEY REFERENCES message(message_id),
    part_count INTEGER NOT NULL,
    type_mask INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    sent_at TEXT,
    content_signature TEXT,
    canonical_post_id INTEGER REFERENCES post(message_id),
    dup_group_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (part_count >= 0),
    CHECK (type_mask >= 0)
) STRICT;

-- 帖级分页/排序的两条主力索引：时间倒序 + message_id 作为键集分页的稳定次序
CREATE INDEX post_time_idx ON post (first_seen_at DESC, message_id DESC);
CREATE INDEX post_sent_idx ON post (sent_at DESC, message_id DESC) WHERE sent_at IS NOT NULL;

-- 折叠与归组各一条部分索引（签名/代表帖为空的行不进索引）
CREATE INDEX post_signature_idx ON post (content_signature)
    WHERE content_signature IS NOT NULL;
CREATE INDEX post_canonical_idx ON post (canonical_post_id)
    WHERE canonical_post_id IS NOT NULL;

-- 回填：一帖一行。只聚合"稳定的构成摘要"，签名与归组留给重算（见文件头）。
-- type_mask 用 sum(**DISTINCT** …)：每个部件的类型是一个 2 的幂，**互不相同**的幂相加才
-- 等于按位或；直接 sum 会把"两张图"加成 2+2=4（正好撞上视频那一位），构成就错了。
INSERT INTO post (message_id, part_count, type_mask, first_seen_at, sent_at)
SELECT
    c.message_id,
    count(*),
    sum(DISTINCT CASE c.item_type
            WHEN 'text' THEN 1
            WHEN 'image' THEN 2
            WHEN 'video' THEN 4
            WHEN 'audio' THEN 8
            WHEN 'link' THEN 16
            WHEN 'sticker' THEN 32
            WHEN 'file' THEN 64
            ELSE 0
        END),
    min(c.first_seen_at),
    (SELECT m.sent_at FROM message m WHERE m.message_id = c.message_id)
  FROM content_item c
 WHERE c.deleted_at IS NULL
 GROUP BY c.message_id;
