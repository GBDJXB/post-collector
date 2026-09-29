-- ===========================================================================
-- 0001_init.sql —— 核心数据模型（第一版，SQLite 方言）
--
-- 本文件是 **SQLite 方言的权威建表脚本**，取代 migrations/postgres/0001_init.sql
-- （PostgreSQL 版随本次迁移删除）。设计依据：DESIGN.md v3.1 §3.1（实体关系）、
-- §3.2「字段级设计（SQLite）」、§3.3（状态机与可见性矩阵）、§5.1（FTS5 双索引）。
--
-- 事实来源与取舍：
--   表 / 列 / 约束 / 索引的**事实来源**是已冻结的 PostgreSQL 版 0001_init.sql；
--   DESIGN §3.2 未覆盖之处按 PG 版补齐。仅有两处以 DESIGN §3.2 为准：
--     (a) media 的从属关系改为 first_seen_content_id（新增 content_media 承担多对多）；
--     (b) 新增 hash_tombstone 表。
--
-- ---------------------------------------------------------------------------
-- 与 PostgreSQL 版的差异清单（逐条对应 DESIGN §3.2 的类型映射）
-- ---------------------------------------------------------------------------
--   [D1] jsonb        → TEXT，配 CHECK (json_valid(col))；允许 NULL 的写成
--                       CHECK (col IS NULL OR json_valid(col))。**不建 GIN 索引**。
--   [D2] text[]       → TEXT（存 JSON 数组），同样配 json_valid 校验。
--                        注意：PG 的空数组默认值 '{}' 在 JSON 里不是合法数组，
--                        本文件一律改写成 '[]'（这是 D2 的必要后果，不是新语义）。
--   [D3] boolean      → INTEGER + CHECK (col IN (0,1))；DEFAULT true/false → 1/0。
--   [D4] timestamptz  → TEXT（ISO-8601 UTC，毫秒精度，字典序即时间序）。
--   [D5] uuid         → TEXT；PG 的 DEFAULT gen_random_uuid() **已删除**，
--                        UUID 一律由应用层生成；UNIQUE 约束保留。
--   [D6] bigint GENERATED ALWAYS AS IDENTITY → INTEGER PRIMARY KEY（rowid 别名）。
--   [D7] smallint / int / bigint（非主键） → INTEGER；numeric / real / double → REAL；
--        bytea → BLOB；inet → TEXT；char(64) → TEXT（STRICT 表只认 INTEGER/TEXT/REAL/BLOB/ANY）。
--   [D8] CREATE EXTENSION IF NOT EXISTS pg_trgm; 已删除。
--   [D9] 使用 gin_trgm_ops 的索引（message_text_trgm_idx）已删除：
--        子串/容错检索改由 FTS5 虚拟表 content_fts 承担（DESIGN §5.1）。
--   [D10] 所有 COMMENT ON 语句已删除（SQLite 无此语句），有价值的中文说明
--         改写为 `--` 行注释。
--   [D11] media.content_id → first_seen_content_id（仅记录首次入库归属）；
--         多对多从属关系由新表 content_media 承担，并补两条索引。
--   [D12] 新增 hash_tombstone：硬删后的哈希墓地，兑现"删除后仍能拦截同一内容
--         被反复导入"的承诺（DESIGN §3.8）。
--   [D13] content_item 新增 text_search / search_extra 两列（PG 版没有，DESIGN
--         §3.2 亦未补）：它们是由应用层维护的派生列。两者与既有 text_content /
--         ocr_text / caption 一起喂给两个 FTS5 external-content 虚拟表；
--         两个虚拟表各配 INSERT / DELETE / UPDATE 三个同步触发器，共 6 个。
--   [D14] 索引名沿用 PG 版（全仓库按名字引用）。PG 版 34 条索引命令中，
--         gin_trgm_ops 那条删除，其余 33 条逐条保留（含 3 条唯一索引）。
--         另按 DESIGN §3.2 补入 PG 版没有的 content_item_time_idx、
--         content_media_media_idx、content_media_content_idx；DESIGN 提到的
--         content_item_message_idx / tag_assignment_tag_idx /
--         tag_assignment_content_idx / tag_namespace_idx 在 PG 版已存在，不重复建。
--         合计 36 条索引（33 + 3）。
--   [D15] 本文件**不得包含任何 PRAGMA 语句**。连接基线（journal_mode=WAL、
--         foreign_keys=ON、busy_timeout、synchronous、temp_store）由
--         pc.adapters.sqlite 在建立连接时统一执行；写进迁移文件会报错。
--         注意 PRAGMA foreign_keys 默认关闭，必须由适配器打开，否则外键静默失效。
--
-- ---------------------------------------------------------------------------
-- 时间戳默认值的约定（与 PG 版**逐列对齐**）
-- ---------------------------------------------------------------------------
--   对齐原则：PG 版里带 DEFAULT now() 的列，这里一律写成
--     DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
--   覆盖各表的 created_at / updated_at，以及 granted_at、started_at、assigned_at、
--   run_after、received_at、ingested_at、first_seen_at、applied_at（schema_migration）。
--   PG 版里**没有**默认值的列，这里也一律不给：deleted_at、finished_at、snapshot_at、
--   expires_at、setting_change.applied_at、hash_tombstone.deleted_at。
--   本文件不做任何"顺手收紧或放宽"：默认值的有无必须与 PG 一一对应，否则同一份应用
--   代码在两个引擎上的插入行为会不一样。
--   updated_at 由应用层在 UPDATE 时重写，不建触发器（沿用 PG 版偏差[5]：少一层隐式行为）。
--
-- ---------------------------------------------------------------------------
-- 执行语义
-- ---------------------------------------------------------------------------
--   * 整篇在**单个事务内执行**，失败整体回滚——由 pc.adapters.sqlite.apply_migration
--     保证（BEGIN … COMMIT）。因此本文件内不写 BEGIN / COMMIT。
--   * 最低 SQLite 3.40（STRICT 表需 3.37、FTS5 trigram 需 3.34）。
--   * 不要在 Python 里用 text.split(';') 切分语句，用 sqlite3.complete_statement()。
--
-- 表清单（34 张真实表 + 2 个 FTS5 虚拟表）：
--   身份权限  schema_migration, app_user, account_identity, api_credential, role,
--             permission, role_binding, storage_backend, source, source_authorization,
--             sync_cursor, fetch_log, user_source_scope
--   内容单元  message, inbox_item, content_item, media, content_media, hash_tombstone, link
--   标签模型  tag, tag_synonym, tag_relation, model_version, classification_run, tag_assignment
--   队列审计  job, review_task, audit_log
--   设置容量  setting, setting_change, retention_policy, storage_usage, retention_run
--   检索虚拟表 content_fts（trigram）, content_word（unicode61）
--
--   刻意缺席（阶段 2 才需要；缺席本身是「无外发」安全红线的一部分，有测试盯着）：
--     content_embedding、ai_provider_policy、egress_log。
--
-- 命名：表名与列名一个都没有改，包括三处为避开保留字而做的改名
--   user → app_user、before/after → before_state/after_state、
--   authorization → source_authorization（详见 PG 版文件头偏差[1][2][7]）。
-- ===========================================================================


-- ===========================================================================
-- 〇、迁移记账
-- ===========================================================================

-- 迁移版本记账；checksum 便于发现已应用文件被改动
CREATE TABLE IF NOT EXISTS schema_migration (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    checksum   TEXT,
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    note       TEXT
) STRICT;


-- ===========================================================================
-- 一、身份、权限与来源（DESIGN §3.2「user / identity / 权限」）
-- ===========================================================================

-- 系统用户。DESIGN 中名为 user，因保留字改为 app_user（PG 版偏差[1]）。
-- tier=0 必须有口令 + TOTP；tier=1 由 root 创建并逐个授权；tier=2 不产生 user 行。
CREATE TABLE app_user (
    user_id         INTEGER PRIMARY KEY,
    uuid            TEXT    NOT NULL UNIQUE,          -- 应用层生成，无数据库默认值
    username        TEXT    UNIQUE,
    display_name    TEXT,
    tier            INTEGER NOT NULL DEFAULT 0,       -- 0=root 1=designated 2=anonymous(不建账号)
    status          TEXT    NOT NULL DEFAULT 'active',
    password_hash   TEXT,
    totp_secret_enc BLOB,
    created_by      INTEGER REFERENCES app_user (user_id),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),  -- UPDATE 时由应用层重写（偏差[5]：不建触发器）
    disabled_at     TEXT,
    last_login_at   TEXT,
    CONSTRAINT app_user_tier_chk   CHECK (tier IN (0, 1, 2)),
    CONSTRAINT app_user_status_chk CHECK (status IN ('active', 'disabled')),
    CONSTRAINT app_user_root_requires_secrets
        CHECK (tier <> 0 OR (password_hash IS NOT NULL AND totp_secret_enc IS NOT NULL))
) STRICT;


-- 一个用户绑定多个平台身份；L1 用户的登录身份与平台发送者身份必须显式绑定（DESIGN §6.1）
CREATE TABLE account_identity (
    identity_id      INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES app_user (user_id),
    platform         TEXT    NOT NULL,
    platform_user_id TEXT    NOT NULL,
    display_name     TEXT,
    is_bot           INTEGER NOT NULL DEFAULT 0,
    verified_at      TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT account_identity_is_bot_chk CHECK (is_bot IN (0, 1)),
    UNIQUE (platform, platform_user_id)
) STRICT;


-- 面向自动化的 Bearer token 凭证；一次性展示，只存哈希（DESIGN §6.1）
CREATE TABLE api_credential (
    cred_id      INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES app_user (user_id),
    name         TEXT    NOT NULL,
    key_hash     TEXT    NOT NULL,                    -- 系统自身密钥只存哈希
    key_prefix   TEXT    NOT NULL,                    -- 便于识别与轮换，不可逆推原文
    scopes       TEXT    NOT NULL DEFAULT '[]',       -- JSON 数组（原 text[]）
    last_used_at TEXT,
    expires_at   TEXT,
    revoked_at   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT api_credential_scopes_json_chk CHECK (json_valid(scopes))
) STRICT;


CREATE TABLE role (
    role_id     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;


-- 权限码按层预置，不使用通配符权限（DESIGN §3.2）：
--   source:read / content:submit / content:delete / inbox:promote / model:invoke / storage:manage …
CREATE TABLE permission (
    perm_id     INTEGER PRIMARY KEY,
    code        TEXT    NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;


CREATE TABLE role_binding (
    binding_id INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user (user_id),
    role_id    INTEGER NOT NULL REFERENCES role (role_id),
    scope_type TEXT,
    scope_id   INTEGER,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (user_id, role_id, scope_type, scope_id)
) STRICT;


-- 存储后端（媒体表引用它；本地目录是默认实现）
-- config：平台特有配置（JSON）；凭证以引用方式存放，禁止明文（DESIGN §6.1）
CREATE TABLE storage_backend (
    backend_id    INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    kind          TEXT    NOT NULL,                   -- local|s3|smb_nfs|webdav|cloud_drive
    config        TEXT    NOT NULL DEFAULT '{}',
    root_prefix   TEXT,
    is_default    INTEGER NOT NULL DEFAULT 0,
    writable      INTEGER NOT NULL DEFAULT 1,
    health_status TEXT,
    last_check_at TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT storage_backend_kind_chk       CHECK (kind IN ('local', 's3', 'smb_nfs', 'webdav', 'cloud_drive')),
    CONSTRAINT storage_backend_is_default_chk CHECK (is_default IN (0, 1)),
    CONSTRAINT storage_backend_writable_chk   CHECK (writable IN (0, 1)),
    CONSTRAINT storage_backend_config_json_chk CHECK (json_valid(config))
) STRICT;


-- 一个来源 = 一条采集通道；必须同时具备授权凭证 + 授权范围 + 授权证明记录才可入库（DESIGN §1.3）
CREATE TABLE source (
    source_id    INTEGER PRIMARY KEY,
    uuid         TEXT    NOT NULL UNIQUE,             -- 应用层生成，无数据库默认值
    name         TEXT    NOT NULL,
    platform     TEXT    NOT NULL,
    kind         TEXT    NOT NULL,                    -- bot_api|webhook|rss|export|user_api
    external_ref TEXT,                                -- 频道 ID / 群 ID / RSS URL
    config       TEXT    NOT NULL DEFAULT '{}',       -- 平台特有配置 JSON（凭证引用，不存明文）
    trust_level  INTEGER NOT NULL DEFAULT 3,          -- 1=可信自有 2=已授权群 3=外部不确定
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER REFERENCES app_user (user_id),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deleted_at   TEXT,
    CONSTRAINT source_kind_chk   CHECK (kind IN ('bot_api', 'webhook', 'rss', 'export', 'user_api')),
    CONSTRAINT source_trust_chk  CHECK (trust_level IN (1, 2, 3)),
    CONSTRAINT source_enabled_chk CHECK (enabled IN (0, 1)),
    CONSTRAINT source_config_json_chk CHECK (json_valid(config))
) STRICT;

CREATE INDEX source_platform_idx ON source (platform) WHERE deleted_at IS NULL;


-- 表名 authorization → source_authorization：PG 版偏差[7]，SQLite 同样保留该名字
CREATE TABLE source_authorization (
    authz_id     INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES source (source_id),
    mode         TEXT    NOT NULL,                    -- owner_token|admin_consent|public_feed|manual_export
    scope        TEXT    NOT NULL DEFAULT '{}',       -- 允许读取的消息类型、时间范围（JSON）
    evidence_ref TEXT,                                -- 授权证明文件路径/链接/截图 hash
    granted_by   INTEGER REFERENCES app_user (user_id),
    granted_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at   TEXT,
    revoked_at   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT source_authorization_mode_chk
        CHECK (mode IN ('owner_token', 'admin_consent', 'public_feed', 'manual_export')),
    CONSTRAINT source_authorization_scope_json_chk CHECK (json_valid(scope))
) STRICT;

CREATE INDEX source_authorization_source_idx ON source_authorization (source_id) WHERE revoked_at IS NULL;


-- 每个来源一条，调度器按来源独立节拍触发，互不阻塞（DESIGN §2.3）
CREATE TABLE sync_cursor (
    source_id            INTEGER PRIMARY KEY REFERENCES source (source_id),
    cursor_value         TEXT,
    last_seen_at         TEXT,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TEXT,
    state                TEXT    NOT NULL DEFAULT 'idle',
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT sync_cursor_state_chk CHECK (state IN ('idle', 'running', 'backoff', 'paused', 'error'))
) STRICT;


CREATE TABLE fetch_log (
    fetch_id             INTEGER PRIMARY KEY,
    source_id            INTEGER NOT NULL REFERENCES source (source_id),
    started_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at          TEXT,
    items_seen           INTEGER,
    items_new            INTEGER,
    http_status          INTEGER,
    error_class          TEXT,
    error_detail         TEXT,
    rate_limit_remaining INTEGER
) STRICT;

CREATE INDEX fetch_log_source_idx ON fetch_log (source_id, started_at DESC);


-- L1 用户可读写的来源范围；权限变更与撤销立即生效（DESIGN §6.1）
CREATE TABLE user_source_scope (
    scope_id   INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user (user_id),
    source_id  INTEGER NOT NULL REFERENCES source (source_id),
    can_read   INTEGER NOT NULL DEFAULT 1,
    can_submit INTEGER NOT NULL DEFAULT 0,
    granted_by INTEGER REFERENCES app_user (user_id),
    granted_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    revoked_at TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT user_source_scope_read_chk   CHECK (can_read IN (0, 1)),
    CONSTRAINT user_source_scope_submit_chk CHECK (can_submit IN (0, 1)),
    UNIQUE (user_id, source_id)
) STRICT;


-- ===========================================================================
-- 二、原始信封与内容单元（DESIGN §3.2）
-- ===========================================================================

CREATE TABLE message (
    message_id           INTEGER PRIMARY KEY,
    uuid                 TEXT    NOT NULL UNIQUE,     -- 应用层生成，无数据库默认值
    source_id            INTEGER NOT NULL REFERENCES source (source_id),
    platform_msg_id      TEXT    NOT NULL,
    platform_thread_id   TEXT,
    sender_identity_id   INTEGER REFERENCES account_identity (identity_id),
    sender_display_name  TEXT,
    sender_is_bot        INTEGER NOT NULL DEFAULT 0,
    sent_at              TEXT,
    ingested_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    message_type         TEXT    NOT NULL DEFAULT 'text',
    text_raw             TEXT,                        -- 不可变原文
    text_normalized      TEXT,                        -- 派生
    lang                 TEXT,                        -- 派生
    reply_to_platform_id TEXT,
    repost_of_message_id INTEGER REFERENCES message (message_id),
    forward_chain        TEXT    NOT NULL DEFAULT '[]',   -- JSON: [{platform, sender, channel, url}]
    permalink            TEXT,
    raw_payload          TEXT,                        -- 原始载荷 JSON，保留可重放（保留策略见 DESIGN §3.2）
    raw_payload_hash     TEXT,
    source_trust         INTEGER,
    nsfw_gate            INTEGER,                     -- 摄入时的门控判定副本
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deleted_at           TEXT,
    CONSTRAINT message_type_chk
        CHECK (message_type IN ('text', 'media', 'mixed', 'link', 'system', 'repost')),
    CONSTRAINT message_sender_is_bot_chk CHECK (sender_is_bot IN (0, 1)),
    CONSTRAINT message_forward_chain_json_chk CHECK (json_valid(forward_chain)),
    CONSTRAINT message_raw_payload_json_chk
        CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
    -- 幂等第一层：重复投递直接忽略（DESIGN §2.3）
    CONSTRAINT message_source_platform_key UNIQUE (source_id, platform_msg_id)
) STRICT;

CREATE INDEX message_source_sent_idx ON message (source_id, sent_at DESC);
CREATE INDEX message_sender_sent_idx ON message (sender_identity_id, sent_at DESC);
-- 原 pg_trgm GIN 索引（message_text_trgm_idx）取消：中文子串/容错检索改由
-- FTS5 的 content_fts 承担（DESIGN §5.1）。


-- L2 匿名提报隔离区。与 content_item 刻意不共用表：审查前零副作用、拒绝后不留垃圾行（DESIGN §3.1）。
-- 媒体字节存放于隔离区前缀（默认 <data_root>/quarantine/inbox/），与内容寻址主库物理分离，
-- 拒绝或超期后由清理任务删除。结构上本表没有任何指向 content_item 的列或外键。
CREATE TABLE inbox_item (
    inbox_id            INTEGER PRIMARY KEY,
    uuid                TEXT    NOT NULL UNIQUE,      -- 应用层生成，无数据库默认值
    received_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    channel             TEXT    NOT NULL,             -- web|telegram_bot|email|api
    submitter_ip_hash   TEXT,                         -- 只存带盐哈希，不存原始 IP
    submitter_ua_hash   TEXT,
    claimed_identity    TEXT    NOT NULL DEFAULT '{}',  -- 提交者自称身份 JSON，不可信，仅供人看
    text_raw            TEXT,
    media_blobs         TEXT    NOT NULL DEFAULT '[]',  -- 隔离区对象键 JSON 数组，非主存储
    blob_sha256         TEXT,
    size_bytes          INTEGER,
    quota_bucket        TEXT,                         -- 配额与限流的桶键（IP/会话）
    scan_verdict        TEXT    NOT NULL DEFAULT '{}',  -- 扫描结果 JSON，fail closed
    status              TEXT    NOT NULL DEFAULT 'pending',
    reviewed_by         INTEGER REFERENCES app_user (user_id),
    reviewed_at         TEXT,
    reject_reason       TEXT,
    promoted_message_id INTEGER REFERENCES message (message_id),
    reject_ttl_at       TEXT,                         -- 拒绝/超期后清理隔离区字节
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT inbox_item_channel_chk CHECK (channel IN ('web', 'telegram_bot', 'email', 'api')),
    CONSTRAINT inbox_item_status_chk
        CHECK (status IN ('pending', 'reviewing', 'promoted', 'rejected', 'expired')),
    CONSTRAINT inbox_item_claimed_identity_json_chk CHECK (json_valid(claimed_identity)),
    CONSTRAINT inbox_item_media_blobs_json_chk      CHECK (json_valid(media_blobs)),
    CONSTRAINT inbox_item_scan_verdict_json_chk     CHECK (json_valid(scan_verdict))
) STRICT;

CREATE INDEX inbox_item_status_idx ON inbox_item (status, received_at);
-- 同一待审内容不重复占位
CREATE UNIQUE INDEX inbox_item_pending_blob_key
    ON inbox_item (blob_sha256) WHERE status = 'pending' AND blob_sha256 IS NOT NULL;


-- 可检索、可打标、可去重的核心单元。
-- text_search / search_extra 是 PG 版没有的派生列（由应用层维护）：
--   text_search —— 应用层分词结果（jieba / pkuseg，空格连接），喂给 content_word；
--                 2 字中文查询必须走词级索引（DESIGN §5.1）。
--   search_extra —— 检索字段合成的补充来源（tag 名、同义词、来源名、发送者名、
--                   链接标题/描述等），喂给 content_fts。
CREATE TABLE content_item (
    content_id       INTEGER PRIMARY KEY,
    uuid             TEXT    NOT NULL UNIQUE,         -- 应用层生成，无数据库默认值
    message_id       INTEGER NOT NULL REFERENCES message (message_id),
    item_type        TEXT    NOT NULL,                -- text|image|video|audio|link|sticker|file
    ordinal          INTEGER NOT NULL DEFAULT 0,      -- 同消息内顺序
    text_content     TEXT,                            -- 文本项正文或媒体项说明文本
    ocr_text         TEXT,                            -- 派生
    caption          TEXT,                            -- 派生（VLM/LLM）
    text_search      TEXT,                            -- 派生：应用层分词结果（D13）
    search_extra     TEXT,                            -- 派生：检索合成补充字段（D13）
    content_hash     TEXT,                            -- 文本项规范化后的 SHA-256
    phash            INTEGER,                         -- 感知哈希（图片项，64 位有符号）
    dhash            INTEGER,
    ahash            INTEGER,
    embedding_id     TEXT,                            -- 阶段 2 接线
    vector_ref       TEXT,
    canonical_id     INTEGER REFERENCES content_item (content_id),  -- 归并后的代表项
    dup_group_id     INTEGER,
    dup_distance     INTEGER,
    nsfw_level       INTEGER NOT NULL DEFAULT 0,      -- 0 safe … 4 restricted
    nsfw_score       REAL,
    sensitive_flags  TEXT    NOT NULL DEFAULT '[]',   -- JSON 数组：gore|minor_risk|violence|self_harm|politics…
    lang             TEXT,
    char_count       INTEGER,
    byte_size        INTEGER,
    quality_score    REAL,
    status           TEXT    NOT NULL DEFAULT 'active',
    submission_tier  INTEGER NOT NULL DEFAULT 0,      -- 0=root 1=designated 2=anonymous
    submitted_by     INTEGER REFERENCES app_user (user_id),          -- L1 提报者；L2 为 NULL
    inbox_item_id    INTEGER REFERENCES inbox_item (inbox_id),       -- 由收件箱提升时留溯源指针
    egress_state     TEXT    NOT NULL DEFAULT 'local_only',
    egress_scope     TEXT    NOT NULL DEFAULT '{}',   -- 放行到哪些服务商/用途（JSON，见 §6.3）
    char_bucket      TEXT,                            -- 未识别角色时的特征归档桶（§4.9）
    first_seen_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen_at     TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deleted_at       TEXT,
    CONSTRAINT content_item_type_chk
        CHECK (item_type IN ('text', 'image', 'video', 'audio', 'link', 'sticker', 'file')),
    CONSTRAINT content_item_status_chk
        CHECK (status IN ('active', 'quarantined', 'archived', 'deleted')),
    CONSTRAINT content_item_nsfw_chk   CHECK (nsfw_level BETWEEN 0 AND 4),
    CONSTRAINT content_item_tier_chk   CHECK (submission_tier IN (0, 1, 2)),
    -- 与 DESIGN §3.3 的 egress_state 取值表一致（含 expired）
    CONSTRAINT content_item_egress_chk
        CHECK (egress_state IN ('local_only', 'pending', 'cleared', 'denied', 'expired')),
    CONSTRAINT content_item_sensitive_flags_json_chk CHECK (json_valid(sensitive_flags)),
    CONSTRAINT content_item_egress_scope_json_chk     CHECK (json_valid(egress_scope)),
    CONSTRAINT content_item_message_ordinal_key UNIQUE (message_id, ordinal)
) STRICT;

-- 部分唯一索引：排除软删除行，软删后可重新导入同一内容
CREATE UNIQUE INDEX content_item_content_hash_key
    ON content_item (content_hash)
    WHERE content_hash IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX content_item_dup_group_idx   ON content_item (dup_group_id);
CREATE INDEX content_item_nsfw_idx        ON content_item (nsfw_level) WHERE nsfw_level >= 2;
CREATE INDEX content_item_egress_idx      ON content_item (egress_state, nsfw_level);      -- 驱动外发待审队列
CREATE INDEX content_item_tier_status_idx ON content_item (submission_tier, status);        -- 驱动提报审查队列
CREATE INDEX content_item_char_bucket_idx ON content_item (char_bucket) WHERE char_bucket IS NOT NULL;
CREATE INDEX content_item_message_idx     ON content_item (message_id, ordinal);            -- 一条消息 → 它的内容项
-- 浏览列表与「过滤后按时间排序」的支撑索引（DESIGN §3.2 补入）：缺它就只能全表扫描再排序
CREATE INDEX content_item_time_idx        ON content_item (first_seen_at DESC) WHERE deleted_at IS NULL;


-- ---------------------------------------------------------------------------
-- FTS5 双索引（DESIGN §5.1）：external content 表，只存倒排表、不复制正文。
--   content_fts  tokenize='trigram'   —— 子串与容错检索（前缀、错字、任意位置的字串）
--   content_word tokenize='unicode61' —— 应用层分词后的词级检索（2 字中文查询走这里）
-- 注意：external content 表要求索引行与正文行一致；软删、改 tag、重算 caption
--       都要同步更新索引行——这正是下面 6 个触发器的职责。
-- ---------------------------------------------------------------------------

CREATE VIRTUAL TABLE content_fts USING fts5(
    text_content, ocr_text, caption, search_extra,
    content='content_item', content_rowid='content_id', tokenize='trigram');

CREATE VIRTUAL TABLE content_word USING fts5(
    text_search,
    content='content_item', content_rowid='content_id', tokenize='unicode61');

-- content_item → content_fts 同步触发器
CREATE TRIGGER content_fts_ai AFTER INSERT ON content_item BEGIN
  INSERT INTO content_fts(rowid, text_content, ocr_text, caption, search_extra)
  VALUES (new.content_id, new.text_content, new.ocr_text, new.caption, new.search_extra);
END;
CREATE TRIGGER content_fts_ad AFTER DELETE ON content_item BEGIN
  INSERT INTO content_fts(content_fts, rowid, text_content, ocr_text, caption, search_extra)
  VALUES ('delete', old.content_id, old.text_content, old.ocr_text, old.caption, old.search_extra);
END;
CREATE TRIGGER content_fts_au AFTER UPDATE ON content_item BEGIN
  INSERT INTO content_fts(content_fts, rowid, text_content, ocr_text, caption, search_extra)
  VALUES ('delete', old.content_id, old.text_content, old.ocr_text, old.caption, old.search_extra);
  INSERT INTO content_fts(rowid, text_content, ocr_text, caption, search_extra)
  VALUES (new.content_id, new.text_content, new.ocr_text, new.caption, new.search_extra);
END;

-- content_item → content_word 同步触发器
CREATE TRIGGER content_word_ai AFTER INSERT ON content_item BEGIN
  INSERT INTO content_word(rowid, text_search)
  VALUES (new.content_id, new.text_search);
END;
CREATE TRIGGER content_word_ad AFTER DELETE ON content_item BEGIN
  INSERT INTO content_word(content_word, rowid, text_search)
  VALUES ('delete', old.content_id, old.text_search);
END;
CREATE TRIGGER content_word_au AFTER UPDATE ON content_item BEGIN
  INSERT INTO content_word(content_word, rowid, text_search)
  VALUES ('delete', old.content_id, old.text_search);
  INSERT INTO content_word(rowid, text_search)
  VALUES (new.content_id, new.text_search);
END;


-- 媒体元数据与指针；字节只在存储后端。删除元数据不删本体（除非显式硬删），
-- 两者由后台一致性任务对账（DESIGN §3.6）。
-- first_seen_content_id 仅记录"首次入库归属"，**不是**唯一的从属关系；
-- 一个媒体本体（sha256 唯一）被多条内容项引用时，引用关系落在 content_media。
CREATE TABLE media (
    media_id              INTEGER PRIMARY KEY,
    uuid                  TEXT    NOT NULL UNIQUE,    -- 应用层生成，无数据库默认值
    first_seen_content_id INTEGER REFERENCES content_item (content_id),
    sha256                TEXT    NOT NULL UNIQUE,
    blake3                TEXT,
    size_bytes            INTEGER NOT NULL,
    mime_detected         TEXT,                       -- 魔数嗅探结果，以此为准
    mime_declared         TEXT,                       -- 仅作参考
    extension             TEXT,
    width                 INTEGER,
    height                INTEGER,
    duration_ms           INTEGER,
    frame_count           INTEGER,
    is_animated           INTEGER NOT NULL DEFAULT 0,
    storage_backend_id    INTEGER REFERENCES storage_backend (backend_id),
    object_key            TEXT    NOT NULL,           -- ab/cd/<sha256>，内容寻址
    storage_class         TEXT    NOT NULL DEFAULT 'hot',
    thumb_key             TEXT,
    preview_key           TEXT,
    webp_key              TEXT,
    exif                  TEXT    NOT NULL DEFAULT '{}',   -- JSON，已清洗敏感字段
    scan_status           TEXT    NOT NULL DEFAULT 'pending',
    scan_engine           TEXT,
    scan_at               TEXT,
    quarantine_key        TEXT,
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deleted_at            TEXT,
    CONSTRAINT media_is_animated_chk  CHECK (is_animated IN (0, 1)),
    CONSTRAINT media_storage_class_chk CHECK (storage_class IN ('hot', 'cold', 'archive')),
    CONSTRAINT media_scan_status_chk
        CHECK (scan_status IN ('pending', 'clean', 'infected', 'suspicious', 'skipped')),
    CONSTRAINT media_exif_json_chk CHECK (json_valid(exif)),
    CONSTRAINT media_backend_object_key UNIQUE (storage_backend_id, object_key)
) STRICT;

CREATE INDEX media_content_idx ON media (first_seen_content_id);   -- 原 media(content_id)，列已改名
CREATE INDEX media_scan_idx    ON media (scan_status) WHERE scan_status <> 'clean';


-- 多对多联结表：一个媒体本体（sha256 唯一）可以被多条内容项引用（DESIGN §3.2、§3.8）。
-- 删除内容项时必须先删本表属于它的行，再按 media_id 计算引用计数决定是否删 media 行。
CREATE TABLE content_media (
    content_id INTEGER NOT NULL REFERENCES content_item (content_id),
    media_id   INTEGER NOT NULL REFERENCES media (media_id),
    role       TEXT    NOT NULL DEFAULT 'primary',    -- primary|attachment|thumbnail_source|og_image
    ordinal    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (content_id, media_id, role),
    CONSTRAINT content_media_role_chk
        CHECK (role IN ('primary', 'attachment', 'thumbnail_source', 'og_image'))
) STRICT;

CREATE INDEX content_media_media_idx   ON content_media (media_id);            -- 反向查询与引用计数
CREATE INDEX content_media_content_idx ON content_media (content_id, ordinal);


-- 硬删后的哈希墓地：兑现"删除后仍能拦截同一内容被反复导入"的承诺（DESIGN §3.8）。
-- 只存哈希、算法与时间，不存任何可关联到内容的元数据；保留期可配（expires_at）。
CREATE TABLE hash_tombstone (
    hash       TEXT NOT NULL,
    alg        TEXT NOT NULL DEFAULT 'sha256',        -- alg: sha256|content_hash
    deleted_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (hash, alg)
) STRICT;


-- 链接默认只记录不抓取；抓取走沙箱抓取器（DESIGN §4.3、§6.4）
CREATE TABLE link (
    link_id           INTEGER PRIMARY KEY,
    content_id        INTEGER REFERENCES content_item (content_id),
    url_raw           TEXT    NOT NULL,
    url_normalized    TEXT,
    url_canonical     TEXT,
    domain            TEXT,
    scheme            TEXT,
    final_url         TEXT,
    redirect_chain    TEXT    NOT NULL DEFAULT '[]',  -- JSON
    fetch_status      TEXT,
    http_status       INTEGER,
    content_type      TEXT,
    fetched_at        TEXT,
    size_bytes        INTEGER,
    title             TEXT,
    description       TEXT,
    site_name         TEXT,
    og_image_media_id INTEGER REFERENCES media (media_id),
    snapshot_key      TEXT,                           -- 本地/Wayback 快照引用
    risk_level        INTEGER NOT NULL DEFAULT 0,     -- 0 未知 … 3 高危
    risk_reasons      TEXT    NOT NULL DEFAULT '[]',  -- JSON 数组
    blocked_reason    TEXT,
    expires_at        TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    deleted_at        TEXT,
    CONSTRAINT link_risk_chk CHECK (risk_level BETWEEN 0 AND 3),
    CONSTRAINT link_redirect_chain_json_chk CHECK (json_valid(redirect_chain)),
    CONSTRAINT link_risk_reasons_json_chk   CHECK (json_valid(risk_reasons))
) STRICT;

CREATE INDEX link_content_idx ON link (content_id);
CREATE INDEX link_domain_idx  ON link (domain);


-- ===========================================================================
-- 三、标签体系与模型运行（DESIGN §3.2、§4.8）
-- ===========================================================================

CREATE TABLE tag (
    tag_id       INTEGER PRIMARY KEY,
    uuid         TEXT    NOT NULL UNIQUE,             -- 应用层生成，无数据库默认值
    name         TEXT    NOT NULL,
    name_norm    TEXT    NOT NULL UNIQUE,             -- 小写、全半角、下划线统一
    display_name TEXT,
    namespace    TEXT    NOT NULL,                    -- 见 §4.8 的十个命名空间
    kind         TEXT    NOT NULL DEFAULT 'auto',
    parent_id    INTEGER REFERENCES tag (tag_id),
    description  TEXT,
    aliases      TEXT    NOT NULL DEFAULT '[]',       -- JSON 数组（原 text[]）
    external_ref TEXT    NOT NULL DEFAULT '{}',       -- JSON：danbooru/gelbooru tag id 等
    usage_count  INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'active',
    merged_into  INTEGER REFERENCES tag (tag_id),
    created_by   INTEGER REFERENCES app_user (user_id),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT tag_namespace_chk
        CHECK (namespace IN ('character', 'feature', 'unclassified', 'work', 'artist',
                             'general', 'meta', 'rating', 'source', 'system')),
    CONSTRAINT tag_kind_chk   CHECK (kind IN ('auto', 'human', 'system', 'suggested')),
    CONSTRAINT tag_status_chk CHECK (status IN ('active', 'pending', 'merged', 'deprecated')),
    -- 合并后必须留下指向
    CONSTRAINT tag_merged_needs_target CHECK (status <> 'merged' OR merged_into IS NOT NULL),
    CONSTRAINT tag_aliases_json_chk      CHECK (json_valid(aliases)),
    CONSTRAINT tag_external_ref_json_chk CHECK (json_valid(external_ref))
) STRICT;

-- 标签浏览页按命名空间分组
CREATE INDEX tag_namespace_idx ON tag (namespace) WHERE status = 'active';


CREATE TABLE tag_synonym (
    synonym_id INTEGER PRIMARY KEY,
    tag_id     INTEGER NOT NULL REFERENCES tag (tag_id),
    alias      TEXT    NOT NULL,
    alias_norm TEXT    NOT NULL UNIQUE,
    source     TEXT,
    confidence REAL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;


CREATE TABLE tag_relation (
    rel_id     INTEGER PRIMARY KEY,
    from_tag   INTEGER NOT NULL REFERENCES tag (tag_id),
    to_tag     INTEGER NOT NULL REFERENCES tag (tag_id),
    relation   TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT tag_relation_kind_chk
        CHECK (relation IN ('parent', 'child', 'related', 'conflicts_with')),
    CONSTRAINT tag_relation_key UNIQUE (from_tag, to_tag, relation)
) STRICT;


-- 所有模型输出必须带 model_version 与 prompt_version，
-- 全库重算才是一条 SQL 加一个任务批次（DESIGN §0 判断 4）
CREATE TABLE model_version (
    model_version_id  INTEGER PRIMARY KEY,
    capability        TEXT    NOT NULL,
    provider          TEXT,
    model_name        TEXT    NOT NULL,
    revision          TEXT,
    params            TEXT    NOT NULL DEFAULT '{}',  -- JSON
    prompt_version    TEXT,
    license           TEXT,
    is_local          INTEGER NOT NULL DEFAULT 1,
    cost_in_per_mtok  REAL,
    cost_out_per_mtok REAL,
    active            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT model_version_capability_chk
        CHECK (capability IN ('tagger', 'ocr', 'nsfw', 'embed_image', 'embed_text',
                              'llm', 'vlm', 'manual')),
    CONSTRAINT model_version_is_local_chk CHECK (is_local IN (0, 1)),
    CONSTRAINT model_version_active_chk   CHECK (active IN (0, 1)),
    CONSTRAINT model_version_params_json_chk CHECK (json_valid(params)),
    CONSTRAINT model_version_revision_key UNIQUE (capability, model_name, revision, prompt_version)
) STRICT;


-- 「当前标签是怎么来的」完全可追溯，也让「用新模型重跑哪些内容」变成一次查询（DESIGN §3.2）
CREATE TABLE classification_run (
    run_id           INTEGER PRIMARY KEY,
    content_id       INTEGER NOT NULL REFERENCES content_item (content_id),
    capability       TEXT    NOT NULL,
    model_version_id INTEGER REFERENCES model_version (model_version_id),
    input_ref        TEXT,
    output           TEXT    NOT NULL DEFAULT '{}',   -- JSON
    confidence       REAL,
    latency_ms       INTEGER,
    cost_estimate    REAL,
    status           TEXT    NOT NULL DEFAULT 'succeeded',
    error_class      TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT classification_run_status_chk
        CHECK (status IN ('succeeded', 'failed', 'skipped')),
    CONSTRAINT classification_run_output_json_chk CHECK (json_valid(output))
) STRICT;

CREATE INDEX classification_run_lookup_idx  ON classification_run (capability, model_version_id, created_at);
CREATE INDEX classification_run_content_idx ON classification_run (content_id);


-- suggested 是 LLM 建议的落点：不进检索、不进统计、不参与任何自动决策（DESIGN §4.7.1）
CREATE TABLE tag_assignment (
    assign_id        INTEGER PRIMARY KEY,
    content_id       INTEGER NOT NULL REFERENCES content_item (content_id),
    tag_id           INTEGER NOT NULL REFERENCES tag (tag_id),
    source           TEXT    NOT NULL,                -- auto|human|system|suggested|merged
    confidence       REAL,
    model_version_id INTEGER REFERENCES model_version (model_version_id),
    evidence         TEXT    NOT NULL DEFAULT '{}',   -- JSON：命中位置、分数、被引用的 OCR 片段
    assigned_by      INTEGER REFERENCES app_user (user_id),
    assigned_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    revoked_at       TEXT,                            -- 人工否决即负样本，进评估集
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT tag_assignment_source_chk
        CHECK (source IN ('auto', 'human', 'system', 'suggested', 'merged')),
    -- 自动判定必须可追溯到模型与置信度（DESIGN §4.6 落地约束）
    CONSTRAINT tag_assignment_auto_needs_model
        CHECK (source <> 'auto' OR (model_version_id IS NOT NULL AND confidence IS NOT NULL)),
    CONSTRAINT tag_assignment_evidence_json_chk CHECK (json_valid(evidence)),
    CONSTRAINT tag_assignment_key UNIQUE (content_id, tag_id, source)
) STRICT;

-- 双向部分索引：两个方向都是热路径，且都必须排除已撤销的标注（DESIGN §3.2）
CREATE INDEX tag_assignment_tag_idx     ON tag_assignment (tag_id, content_id) WHERE revoked_at IS NULL;  -- 按标签取内容
CREATE INDEX tag_assignment_content_idx ON tag_assignment (content_id) WHERE revoked_at IS NULL;          -- 组装结果时批量取 tag


-- ===========================================================================
-- 四、任务队列、复核、审计（DESIGN §2.3、§3.3、§6.1）
-- ===========================================================================

-- 用数据库当队列：任务表 + 状态机 + 死信，零额外运维（DESIGN §2.3）。
-- 取任务示例（SQLite 在单写者模型下不需要 FOR UPDATE SKIP LOCKED，
-- 由单进程 worker + BEGIN IMMEDIATE 保证互斥）：
--   UPDATE job SET status='running', locked_by=?, locked_at=?, attempts=attempts+1
--   WHERE job_id = (SELECT job_id FROM job
--                   WHERE status='queued' AND run_after <= ?
--                   ORDER BY priority, run_after LIMIT 1)
--   RETURNING *;
CREATE TABLE job (
    job_id       INTEGER PRIMARY KEY,
    job_type     TEXT    NOT NULL,
    target_type  TEXT    NOT NULL,
    target_id    INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'queued',
    priority     INTEGER NOT NULL DEFAULT 100,        -- 数字越小越优先
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    run_after    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    locked_by    TEXT,
    locked_at    TEXT,
    idem_key     TEXT,                                -- job_type:target_type:target_id:model_version
    last_error   TEXT,
    dead_reason  TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at  TEXT,
    CONSTRAINT job_status_chk
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead', 'canceled'))
) STRICT;

-- 幂等第二层：同一目标同一模型版本只处理一次
CREATE UNIQUE INDEX job_idem_key ON job (idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX job_claim_idx ON job (status, priority, run_after) WHERE status IN ('queued', 'failed');
CREATE INDEX job_dead_idx  ON job (status) WHERE status = 'dead';


-- 三类队列在 UI 上必须分开呈现：提报审查 / 外发放行 / 质量纠错，
-- 决策依据与风险完全不同（DESIGN §2.3）
CREATE TABLE review_task (
    task_id     INTEGER PRIMARY KEY,
    uuid        TEXT    NOT NULL UNIQUE,              -- 应用层生成，无数据库默认值
    kind        TEXT    NOT NULL,
    target_type TEXT    NOT NULL,
    target_id   INTEGER NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 100,
    payload     TEXT    NOT NULL DEFAULT '{}',        -- JSON
    status      TEXT    NOT NULL DEFAULT 'pending',
    assigned_to INTEGER REFERENCES app_user (user_id),
    resolution  TEXT,                                 -- JSON
    resolved_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT review_task_kind_chk
        CHECK (kind IN ('inbox_submission', 'egress_review', 'low_confidence', 'nsfw_uncertain',
                        'dedup_conflict', 'scan_gray', 'tag_conflict', 'link_risk', 'char_ambiguous')),
    CONSTRAINT review_task_status_chk
        CHECK (status IN ('pending', 'in_progress', 'resolved', 'canceled')),
    CONSTRAINT review_task_payload_json_chk    CHECK (json_valid(payload)),
    CONSTRAINT review_task_resolution_json_chk
        CHECK (resolution IS NULL OR json_valid(resolution))
) STRICT;

CREATE INDEX review_task_queue_idx  ON review_task (kind, status, priority, created_at);
CREATE INDEX review_task_target_idx ON review_task (target_type, target_id);


-- 所有特权操作、授权变更、越权尝试都写这里；
-- 外发相关记录在阶段 2 另有 egress_log（DESIGN §6.1、§6.3）
CREATE TABLE audit_log (
    audit_id     INTEGER PRIMARY KEY,
    actor_type   TEXT    NOT NULL,                    -- user|system|connector|llm
    actor_id     INTEGER,
    action       TEXT    NOT NULL,
    target_type  TEXT,
    target_id    INTEGER,
    before_state TEXT,                                -- JSON；DESIGN 写作 before（PG 版偏差[2]）
    after_state  TEXT,                                -- JSON；DESIGN 写作 after（PG 版偏差[2]）
    result       TEXT,
    ip           TEXT,                                -- 原 inet
    user_agent   TEXT,
    request_id   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT audit_log_actor_chk CHECK (actor_type IN ('user', 'system', 'connector', 'llm')),
    CONSTRAINT audit_log_before_state_json_chk
        CHECK (before_state IS NULL OR json_valid(before_state)),
    CONSTRAINT audit_log_after_state_json_chk
        CHECK (after_state IS NULL OR json_valid(after_state))
) STRICT;

CREATE INDEX audit_log_target_idx ON audit_log (target_type, target_id, created_at DESC);
CREATE INDEX audit_log_actor_idx  ON audit_log (actor_type, actor_id, created_at DESC);
CREATE INDEX audit_log_action_idx ON audit_log (action, created_at DESC);


-- ===========================================================================
-- 五、设置层（DESIGN §6.2）
-- ===========================================================================

-- L1 设置页的落地：source 让「这个值从哪来」可被查询，
-- 对应 pc config show --explain（DESIGN §6.2.4）。
-- 设置项的自描述（label/help/impact/range）不在数据库里，
-- 在版本化的 config/settings-registry.yaml 中。
CREATE TABLE setting (
    key               TEXT    PRIMARY KEY,
    value             TEXT,                           -- JSON
    value_type        TEXT    NOT NULL,               -- 与注册表 type 对应
    source            TEXT    NOT NULL DEFAULT 'default',
    schema_version    INTEGER NOT NULL DEFAULT 1,
    updated_by        INTEGER REFERENCES app_user (user_id),
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    is_dangerous      INTEGER NOT NULL DEFAULT 0,
    last_validated_at TEXT,
    CONSTRAINT setting_source_chk CHECK (source IN ('default', 'yaml', 'db', 'env')),
    CONSTRAINT setting_is_dangerous_chk CHECK (is_dangerous IN (0, 1)),
    CONSTRAINT setting_value_json_chk CHECK (value IS NULL OR json_valid(value))
) STRICT;


-- 每次配置改动的前后值与校验结果可追溯；
-- 失败必须回滚而不是把系统卡在不可用状态（DESIGN §6.2.2）
CREATE TABLE setting_change (
    change_id       INTEGER PRIMARY KEY,
    key             TEXT    NOT NULL,
    before_state    TEXT,                             -- JSON；DESIGN 写作 before（PG 版偏差[2]）
    after_state     TEXT,                             -- JSON；DESIGN 写作 after（PG 版偏差[2]）
    changed_by      INTEGER REFERENCES app_user (user_id),
    changed_via     TEXT    NOT NULL,                 -- ui|yaml|env|migration
    precheck_result TEXT    NOT NULL DEFAULT '{}',    -- JSON
    applied         INTEGER NOT NULL DEFAULT 0,
    rolled_back     INTEGER NOT NULL DEFAULT 0,
    applied_at      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT setting_change_via_chk CHECK (changed_via IN ('ui', 'yaml', 'env', 'migration')),
    CONSTRAINT setting_change_applied_chk     CHECK (applied IN (0, 1)),
    CONSTRAINT setting_change_rolled_back_chk CHECK (rolled_back IN (0, 1)),
    CONSTRAINT setting_change_precheck_result_json_chk CHECK (json_valid(precheck_result)),
    CONSTRAINT setting_change_before_state_json_chk
        CHECK (before_state IS NULL OR json_valid(before_state)),
    CONSTRAINT setting_change_after_state_json_chk
        CHECK (after_state IS NULL OR json_valid(after_state))
) STRICT;

CREATE INDEX setting_change_key_idx ON setting_change (key, created_at DESC);


-- ===========================================================================
-- 六、容量管理与保留策略（DESIGN §7.5）
-- ===========================================================================

-- 滚动删除即最大的成本控制手段。dry_run 默认 1：先出报告，确认后才执行（DESIGN §7.5）
CREATE TABLE retention_policy (
    policy_id            INTEGER PRIMARY KEY,
    name                 TEXT    NOT NULL UNIQUE,
    scope_type           TEXT    NOT NULL,
    scope_id             INTEGER,
    strategy             TEXT    NOT NULL,
    retain_days          INTEGER,
    min_keep_count       INTEGER,                     -- 条数下限，保证不删空
    max_total_bytes      INTEGER,
    max_items            INTEGER,
    age_tiebreak         TEXT,                        -- 超限时先删什么
    never_evict_tags     TEXT    NOT NULL DEFAULT '[]',   -- JSON 数组（原 text[]），命中即豁免滚动删除
    never_evict_nsfw_max INTEGER,
    action               TEXT    NOT NULL,
    apply_to             TEXT    NOT NULL DEFAULT '{}',   -- JSON
    dry_run              INTEGER NOT NULL DEFAULT 1,      -- 默认 dry-run，安全阀
    enabled              INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CONSTRAINT retention_scope_chk
        CHECK (scope_type IN ('global', 'source', 'tag', 'nsfw_level', 'submission_tier')),
    CONSTRAINT retention_strategy_chk
        CHECK (strategy IN ('keep_forever', 'ttl', 'rolling_cap', 'size_budget', 'hot_cold_tier')),
    CONSTRAINT retention_tiebreak_chk
        CHECK (age_tiebreak IS NULL OR age_tiebreak IN
               ('oldest', 'least_accessed', 'lowest_quality', 'not_in_any_collection')),
    CONSTRAINT retention_action_chk
        CHECK (action IN ('archive', 'soft_delete', 'hard_delete', 'demote_storage_class')),
    CONSTRAINT retention_dry_run_chk CHECK (dry_run IN (0, 1)),
    CONSTRAINT retention_enabled_chk CHECK (enabled IN (0, 1)),
    CONSTRAINT retention_never_evict_tags_json_chk CHECK (json_valid(never_evict_tags)),
    CONSTRAINT retention_apply_to_json_chk          CHECK (json_valid(apply_to))
) STRICT;


-- 时序快照，驱动水位与「按当前增速还有多少天到水位线」的预测（DESIGN §7.5）
CREATE TABLE storage_usage (
    snapshot_at  TEXT    NOT NULL,
    backend_id   INTEGER NOT NULL REFERENCES storage_backend (backend_id),
    class        TEXT    NOT NULL,
    object_count INTEGER NOT NULL DEFAULT 0,
    total_bytes  INTEGER NOT NULL DEFAULT 0,
    item_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_at, backend_id, class),
    CONSTRAINT storage_usage_class_chk CHECK (class IN ('hot', 'cold', 'archive'))
) STRICT;


-- dry-run 报告与实际执行结果必须可对比：验收标准要求两者一致（DESIGN §9 阶段 1 验收⑨）
CREATE TABLE retention_run (
    run_id        INTEGER PRIMARY KEY,
    policy_id     INTEGER REFERENCES retention_policy (policy_id),
    started_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at   TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 1,
    candidates    INTEGER,
    deleted_media INTEGER,
    freed_bytes   INTEGER,
    blocked_count INTEGER,
    report        TEXT    NOT NULL DEFAULT '{}',      -- JSON
    CONSTRAINT retention_run_dry_run_chk CHECK (dry_run IN (0, 1)),
    CONSTRAINT retention_run_report_json_chk CHECK (json_valid(report))
) STRICT;

CREATE INDEX retention_run_policy_idx ON retention_run (policy_id, started_at DESC);


-- ===========================================================================
-- 迁移记录
-- ===========================================================================

INSERT INTO schema_migration (version, name, note)
VALUES (1, '0001_init', '核心数据模型首版（SQLite）；未包含 content_embedding / ai_provider_policy / egress_log')
ON CONFLICT (version) DO NOTHING;

-- ===========================================================================
-- 后续迁移（占位，勿在此文件继续追加）：
--   0002_embedding.sql        content_embedding + 向量索引（DESIGN §5.3，阶段 2）
--   0003_egress.sql           ai_provider_policy + egress_log（DESIGN §6.3，阶段 2）
--   0004_search_document.sql  content_item 检索合成列的进一步物化与权重（DESIGN §5.1）
-- ===========================================================================
