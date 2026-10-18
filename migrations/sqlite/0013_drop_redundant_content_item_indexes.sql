-- content_item_message_idx 与 UNIQUE(message_id, ordinal) 自动索引完全重复。
-- 帖级按 post.sent_at、Web v2 按 message.sent_at 排序；content_item.sent_at 仍保留
-- 作为可体检、可重算的派生字段，但没有按它过滤或排序的当前查询。
-- 0001/0006 已应用文件不改动，只移除没有当前消费者的显式索引。
DROP INDEX content_item_message_idx;
DROP INDEX content_item_sent_idx;
