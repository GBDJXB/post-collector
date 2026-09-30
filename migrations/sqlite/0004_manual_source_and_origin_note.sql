-- ===========================================================================
-- 0004_manual_source_and_origin_note.sql —— root 手工上传成为一等来源
--
-- 背景：root 本人把文件放进来时，「授权」来自**他本人的操作**，而不是某个平台的
-- 凭证。原来只能用 kind='export' + mode='manual_export' 凑合，但那个组合的语义是
-- "我导出了平台数据"，与"我本人就是来源"是两件事。审计表要说清是哪一件。
--
-- 三处改动：
--   1. source.kind 增加 'manual'（root 手工上传/来源待补的正式归档桶）
--   2. source_authorization.mode 增加 'root_self'（授权来自 root 本人在场操作，无凭证）
--   3. message.origin_note 新列（自由文本来源备注，"我后来想起来了"的钩子）
--
-- **前两处只能重建表**：SQLite 没有 ALTER TABLE DROP CONSTRAINT，而这两个枚举都写在
-- CHECK 约束里。所以本文件带 `pc:foreign_keys=off` 指令——重建期间必须关掉外键，
-- 否则 DROP TABLE source 会做隐式 DELETE 并因 5 张子表的外键而失败。迁移器会在
-- 提交前跑 PRAGMA foreign_key_check 兜底（见 migrations/README.md）。
--
-- 重建后的列、默认值、其它 CHECK、STRICT 全部与 0001 逐字一致，只差那一处枚举取值；
-- tests/test_schema_integration.py 有一条断言专门比对这件事，防止手抄漂移。
--
-- 依据：DESIGN §1.3（接入模式分级）、§3.2（source/source_authorization/message 字段）、
--      §6.1（凭证与授权是两件事）、附录 C。
-- 执行语义：整篇在单个事务内执行，失败整体回滚；本文件不含任何 PRAGMA 语句。
-- ===========================================================================

-- pc:foreign_keys=off

-- ---------------------------------------------------------------------------
-- 1) source：kind 增加 'manual'
-- ---------------------------------------------------------------------------

CREATE TABLE source_rebuild (
    source_id    INTEGER PRIMARY KEY,
    uuid         TEXT    NOT NULL UNIQUE,             -- 应用层生成，无数据库默认值
    name         TEXT    NOT NULL,
    platform     TEXT    NOT NULL,
    kind         TEXT    NOT NULL,                    -- bot_api|webhook|rss|export|user_api|manual
    external_ref TEXT,                                -- 频道 ID / 群 ID / RSS URL
    config       TEXT    NOT NULL DEFAULT '{}',       -- 平台特有配置 JSON（凭证引用，不存明文）
    trust_level  INTEGER NOT NULL DEFAULT 3,          -- 1=可信自有 2=已授权群 3=外部不确定
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER REFERENCES app_user (user_id),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deleted_at   TEXT,
    CONSTRAINT source_kind_chk   CHECK (kind IN ('bot_api', 'webhook', 'rss', 'export', 'user_api', 'manual')),
    CONSTRAINT source_trust_chk  CHECK (trust_level IN (1, 2, 3)),
    CONSTRAINT source_enabled_chk CHECK (enabled IN (0, 1)),
    CONSTRAINT source_config_json_chk CHECK (json_valid(config))
) STRICT;

INSERT INTO source_rebuild SELECT * FROM source;
DROP TABLE source;
ALTER TABLE source_rebuild RENAME TO source;
CREATE INDEX source_platform_idx ON source (platform) WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- 2) source_authorization：mode 增加 'root_self'
-- ---------------------------------------------------------------------------

CREATE TABLE source_authorization_rebuild (
    authz_id     INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES source (source_id),
    mode         TEXT    NOT NULL,                    -- owner_token|admin_consent|public_feed|manual_export|root_self
    scope        TEXT    NOT NULL DEFAULT '{}',       -- 允许读取的消息类型、时间范围（JSON）
    evidence_ref TEXT,                                -- 授权证明文件路径/链接/截图 hash
    granted_by   INTEGER REFERENCES app_user (user_id),
    granted_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at   TEXT,
    revoked_at   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT source_authorization_mode_chk
        CHECK (mode IN ('owner_token', 'admin_consent', 'public_feed', 'manual_export', 'root_self')),
    CONSTRAINT source_authorization_scope_json_chk CHECK (json_valid(scope))
) STRICT;

INSERT INTO source_authorization_rebuild SELECT * FROM source_authorization;
DROP TABLE source_authorization;
ALTER TABLE source_authorization_rebuild RENAME TO source_authorization;
CREATE INDEX source_authorization_source_idx ON source_authorization (source_id) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- 3) message.origin_note：来源备注（可空，root 手工上传时填）
-- ---------------------------------------------------------------------------

ALTER TABLE message ADD COLUMN origin_note TEXT;
