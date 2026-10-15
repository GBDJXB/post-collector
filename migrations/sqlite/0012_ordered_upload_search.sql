-- 本机有序上传的可重放部件清单；旧信封以空数组表示原有文本先于媒体的顺序。
ALTER TABLE message ADD COLUMN ordered_parts TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(ordered_parts));
CREATE INDEX link_url_normalized_idx ON link(url_normalized) WHERE deleted_at IS NULL;
