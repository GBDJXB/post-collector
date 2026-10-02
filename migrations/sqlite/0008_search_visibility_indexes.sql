-- 帖级检索反复读取部件的可见性和 message_id，避免回读含正文的宽表行。
-- message_id 路径用于同帖可见部件与标签判定；content_id 路径用于 FTS/标签候选映射。
-- SQLite 普通索引隐含 rowid，因此第一条也覆盖 tag_assignment 的 content_id 关联。
CREATE INDEX content_item_visibility_idx
    ON content_item (message_id, deleted_at, status, nsfw_level);
CREATE INDEX content_item_lookup_idx
    ON content_item (content_id, message_id, deleted_at, status, nsfw_level);
