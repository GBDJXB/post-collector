-- 帖级查询与折叠守卫只需要消息的可见性/来源，不应回读完整信封正文。
-- 部分索引实时随软删更新；正文和媒体引用仍由主键路径读取。
CREATE INDEX message_visible_idx ON message (message_id, source_id)
    WHERE deleted_at IS NULL;
