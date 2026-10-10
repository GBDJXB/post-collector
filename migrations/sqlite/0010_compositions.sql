-- 独立用户组合；来源消息和内容项归属保持原样。
CREATE TABLE composition (
    composition_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 255),
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
) STRICT;
CREATE TABLE composition_member (
    composition_id INTEGER NOT NULL REFERENCES composition(composition_id),
    position INTEGER NOT NULL CHECK(position >= 0),
    content_id INTEGER NOT NULL REFERENCES content_item(content_id),
    PRIMARY KEY(composition_id, position)
) STRICT;
CREATE INDEX composition_member_content_idx ON composition_member(content_id,composition_id);
CREATE TABLE composition_tag_assignment (
    assign_id INTEGER PRIMARY KEY,
    composition_id INTEGER NOT NULL REFERENCES composition(composition_id),
    tag_id INTEGER NOT NULL REFERENCES tag(tag_id),
    source TEXT NOT NULL DEFAULT 'human' CHECK(source='human'),
    assigned_by INTEGER REFERENCES app_user(user_id),
    assigned_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(composition_id,tag_id,source)
) STRICT;
CREATE INDEX composition_tag_active_idx ON composition_tag_assignment(tag_id,composition_id)
    WHERE revoked_at IS NULL;
-- 旧无序签名失效，重算前各消息独立显示。
UPDATE post SET content_signature=NULL,canonical_post_id=NULL,dup_group_id=NULL;
