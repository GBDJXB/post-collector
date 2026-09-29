-- ===========================================================================
-- 0001_init.sql —— 核心数据模型（第一版）
--
-- 依据：DESIGN.md §3.1（实体关系）、§3.2（字段级设计）、§3.3（状态机与可见性矩阵）。
--
-- 状态：**已于 2026-09-28 首次在真实 PostgreSQL 上执行成功**——修订记录如下。
--
-- 修订记录：
--   2026-09-28  首次真实执行。环境：Docker Desktop / WSL2，postgres:16-alpine
--               （postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea，
--               server 16.15），库编码 UTF8 / locale C，PGDATA 落在 data/db。
--               验证方式：先 scratch 库（pc_verify）试跑，再应用到主库 pc；两次均 psql exit=0，
--               零 ERROR / 零 WARNING。
--               实测计数：32 张表、34 条索引命令（31 普通 + 3 唯一）、25 条 COMMENT、1 条记账 INSERT，
--               pg_trgm 已安装；content_embedding / ai_provider_policy / egress_log 确认缺席（偏差[6]）。
--               本次修正：偏差[7] 表名 authorization → source_authorization。
--               自本次起本文件冻结：后续任何结构改动都以新迁移追加，不再改已应用的版本。
--
-- 通用约定（DESIGN §3.2）：
--   * 主键   BIGINT GENERATED ALWAYS AS IDENTITY；对外暴露用 uuid 列，避免枚举
--   * 时间戳 一律 timestamptz；软删除用 deleted_at timestamptz
--   * 所有表带 created_at / updated_at
--
-- 与 DESIGN 的显式偏差（均在此声明，不在正文里偷偷改变）：
--   [1] 表名 user → app_user：user 是 PostgreSQL 保留字，否则全库都要写 "user"。
--   [2] 列 before / after → before_state / after_state：避免与保留关键字混淆，
--       出现在 audit_log 与 setting_change。
--   [3] §3.1 关系图中的 content_item.duplicate_of 由 §3.2 的
--       canonical_id + dup_group_id 两个列共同实现，不单独建列。
--   [4] 所有「状态类」列用 TEXT + CHECK 约束，而不是 CREATE TYPE 枚举：
--       新增取值只需改 CHECK，不必走 ALTER TYPE 的迁移流程。
--   [5] updated_at 由应用层写入，不建触发器（少一层隐式行为）。
--   [6] 刻意不创建的三张表（阶段 2 才需要，缺席本身就是「无外发」红线的一部分）：
--       content_embedding（向量，§8.1 阶段 2）、ai_provider_policy、egress_log（§6.3）。
--   [7] 表名 authorization → source_authorization：authorization 是 PostgreSQL 保留字
--       （catcode=T，pg_get_keywords()），不引号包裹无法作为表名。约束与索引名一并前缀化。
--       本条为 0001 首次真实执行时发现，见下方「修订记录」。
--
-- 应用方式（两条路径等价，二者都验证过；选一条即可）：
--   容器内 psql（需 psql 客户端）：
--     docker compose exec -T db psql -U pc -d pc -v ON_ERROR_STOP=1 -f /migrations/0001_init.sql
--   纯 Python（无 psql 依赖，容器内同样可用）：
--     uv run pc db apply
--   校验：uv run pc db status   （只读，报告表数、扩展、记账与 checksum 漂移）
--
-- 注意：请**整条语句执行**，不要在 Python 里用 text.split(';') 之类的土办法切分语句。
--   原因有二：(a) 部分 COMMENT 文本内含分号；(b) 多行 COMMENT 使用了 PostgreSQL 的
--   相邻字符串字面量拼接（'a' 'b' 等价于 'ab'），psql 与 psycopg 都支持，
--   但按分号切分的朴素迁移器会切坏。若将来要换迁移器，先改成显式 'a' || 'b' 拼接。
-- ===========================================================================

-- 中文容错检索依赖 pg_trgm（需较高权限；不可用时降级为应用层分词，DESIGN §5.1）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 迁移记账表：由 0001 自己创建，用于后续版本的顺序应用与校验
CREATE TABLE IF NOT EXISTS schema_migration (
    version     integer      PRIMARY KEY,
    name        text         NOT NULL,
    checksum    text,
    applied_at  timestamptz  NOT NULL DEFAULT now(),
    note        text
);

COMMENT ON TABLE schema_migration IS '迁移版本记账；checksum 便于发现已应用文件被改动';


-- ===========================================================================
-- 一、身份、权限与来源（DESIGN §3.2「user / identity / 权限」）
-- ===========================================================================

CREATE TABLE app_user (
    user_id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid           uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    username       text        UNIQUE,
    display_name   text,
    tier           smallint    NOT NULL DEFAULT 0,   -- 0=root 1=designated 2=anonymous(不建账号)
    status         text        NOT NULL DEFAULT 'active',
    password_hash  text,
    totp_secret_enc bytea,
    created_by     bigint      REFERENCES app_user (user_id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    disabled_at    timestamptz,
    last_login_at  timestamptz,
    CONSTRAINT app_user_tier_chk   CHECK (tier IN (0, 1, 2)),
    CONSTRAINT app_user_status_chk CHECK (status IN ('active', 'disabled')),
    -- tier=0 必须有口令 + TOTP；tier=2 不产生 user 行（由应用层与入口配额控制）
    CONSTRAINT app_user_root_requires_secrets
        CHECK (tier <> 0 OR (password_hash IS NOT NULL AND totp_secret_enc IS NOT NULL))
);

COMMENT ON TABLE app_user IS '系统用户。DESIGN 中名为 user，因保留字改为 app_user（偏差[1]）';


CREATE TABLE account_identity (
    identity_id      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          bigint      NOT NULL REFERENCES app_user (user_id),
    platform         text        NOT NULL,
    platform_user_id text        NOT NULL,
    display_name     text,
    is_bot           boolean     NOT NULL DEFAULT false,
    verified_at      timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform, platform_user_id)
);

COMMENT ON TABLE account_identity IS '一个用户绑定多个平台身份；L1 用户的登录身份与平台发送者身份必须显式绑定（DESIGN §6.1）';


CREATE TABLE api_credential (
    cred_id      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      bigint      NOT NULL REFERENCES app_user (user_id),
    name         text        NOT NULL,
    key_hash     text        NOT NULL,            -- 系统自身密钥只存哈希
    key_prefix   text        NOT NULL,            -- 便于识别与轮换，不可逆推原文
    scopes       text[]      NOT NULL DEFAULT '{}',
    last_used_at timestamptz,
    expires_at   timestamptz,
    revoked_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE api_credential IS '面向自动化的 Bearer token 凭证；一次性展示，只存哈希（DESIGN §6.1）';


CREATE TABLE role (
    role_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE permission (
    perm_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        text        NOT NULL UNIQUE,   -- source:read / content:submit / inbox:promote …
    description text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE permission IS '权限码按层预置，不使用通配符权限（DESIGN §3.2）';


CREATE TABLE role_binding (
    binding_id bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES app_user (user_id),
    role_id    bigint      NOT NULL REFERENCES role (role_id),
    scope_type text,
    scope_id   bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, role_id, scope_type, scope_id)
);


-- 存储后端（媒体表引用它；本地目录是默认实现）
CREATE TABLE storage_backend (
    backend_id    bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text        NOT NULL UNIQUE,
    kind          text        NOT NULL,            -- local|s3|smb_nfs|webdav|cloud_drive
    config        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    root_prefix   text,
    is_default    boolean     NOT NULL DEFAULT false,
    writable      boolean     NOT NULL DEFAULT true,
    health_status text,
    last_check_at timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT storage_backend_kind_chk CHECK (kind IN ('local', 's3', 'smb_nfs', 'webdav', 'cloud_drive'))
);

COMMENT ON COLUMN storage_backend.config IS '平台特有配置；凭证以引用方式存放，禁止明文（DESIGN §6.1）';


CREATE TABLE source (
    source_id    bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid         uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    name         text        NOT NULL,
    platform     text        NOT NULL,
    kind         text        NOT NULL,             -- bot_api|webhook|rss|export|user_api
    external_ref text,                             -- 频道 ID / 群 ID / RSS URL
    config       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    trust_level  smallint    NOT NULL DEFAULT 3,   -- 1=可信自有 2=已授权群 3=外部不确定
    enabled      boolean     NOT NULL DEFAULT true,
    created_by   bigint      REFERENCES app_user (user_id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz,
    CONSTRAINT source_kind_chk  CHECK (kind IN ('bot_api', 'webhook', 'rss', 'export', 'user_api')),
    CONSTRAINT source_trust_chk CHECK (trust_level IN (1, 2, 3))
);

COMMENT ON TABLE source IS '一个来源 = 一条采集通道；必须同时具备授权凭证 + 授权范围 + 授权证明记录才可入库（DESIGN §1.3）';

CREATE INDEX source_platform_idx ON source (platform) WHERE deleted_at IS NULL;


CREATE TABLE source_authorization (
    authz_id    bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id   bigint      NOT NULL REFERENCES source (source_id),
    mode        text        NOT NULL,              -- owner_token|admin_consent|public_feed|manual_export
    scope       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    evidence_ref text,                             -- 授权证明文件路径/链接/截图 hash
    granted_by  bigint      REFERENCES app_user (user_id),
    granted_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz,
    revoked_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_authorization_mode_chk
        CHECK (mode IN ('owner_token', 'admin_consent', 'public_feed', 'manual_export'))
);

CREATE INDEX source_authorization_source_idx ON source_authorization (source_id) WHERE revoked_at IS NULL;


CREATE TABLE sync_cursor (
    source_id            bigint      PRIMARY KEY REFERENCES source (source_id),
    cursor_value         text,
    last_seen_at         timestamptz,
    last_success_at      timestamptz,
    consecutive_failures integer     NOT NULL DEFAULT 0,
    next_attempt_at      timestamptz,
    state                text        NOT NULL DEFAULT 'idle',
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sync_cursor_state_chk CHECK (state IN ('idle', 'running', 'backoff', 'paused', 'error'))
);

COMMENT ON TABLE sync_cursor IS '每个来源一条，调度器按来源独立节拍触发，互不阻塞（DESIGN §2.3）';


CREATE TABLE fetch_log (
    fetch_id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id            bigint      NOT NULL REFERENCES source (source_id),
    started_at           timestamptz NOT NULL DEFAULT now(),
    finished_at          timestamptz,
    items_seen           integer,
    items_new            integer,
    http_status          integer,
    error_class          text,
    error_detail         text,
    rate_limit_remaining integer
);

CREATE INDEX fetch_log_source_idx ON fetch_log (source_id, started_at DESC);


CREATE TABLE user_source_scope (
    scope_id   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES app_user (user_id),
    source_id  bigint      NOT NULL REFERENCES source (source_id),
    can_read   boolean     NOT NULL DEFAULT true,
    can_submit boolean     NOT NULL DEFAULT false,
    granted_by bigint      REFERENCES app_user (user_id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, source_id)
);

COMMENT ON TABLE user_source_scope IS 'L1 用户可读写的来源范围；权限变更与撤销立即生效（DESIGN §6.1）';


-- ===========================================================================
-- 二、原始信封与内容单元（DESIGN §3.2）
-- ===========================================================================

CREATE TABLE message (
    message_id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid                uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    source_id           bigint      NOT NULL REFERENCES source (source_id),
    platform_msg_id     text        NOT NULL,
    platform_thread_id  text,
    sender_identity_id  bigint      REFERENCES account_identity (identity_id),
    sender_display_name text,
    sender_is_bot       boolean     NOT NULL DEFAULT false,
    sent_at             timestamptz,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    message_type        text        NOT NULL DEFAULT 'text',
    text_raw            text,                              -- 不可变原文
    text_normalized     text,                              -- 派生
    lang                text,                              -- 派生
    reply_to_platform_id text,
    repost_of_message_id bigint     REFERENCES message (message_id),
    forward_chain       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    permalink           text,
    raw_payload         jsonb,                             -- 原始载荷，保留可重放
    raw_payload_hash    text,
    source_trust        smallint,
    nsfw_gate           smallint,                          -- 摄入时的门控判定副本
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    CONSTRAINT message_type_chk
        CHECK (message_type IN ('text', 'media', 'mixed', 'link', 'system', 'repost')),
    -- 幂等第一层：重复投递直接忽略（DESIGN §2.3）
    CONSTRAINT message_source_platform_key UNIQUE (source_id, platform_msg_id)
);

CREATE INDEX message_source_sent_idx   ON message (source_id, sent_at DESC);
CREATE INDEX message_sender_sent_idx   ON message (sender_identity_id, sent_at DESC);
CREATE INDEX message_text_trgm_idx     ON message USING gin (text_normalized gin_trgm_ops);


CREATE TABLE inbox_item (
    inbox_id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid                uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    received_at         timestamptz NOT NULL DEFAULT now(),
    channel             text        NOT NULL,               -- web|telegram_bot|email|api
    submitter_ip_hash   text,                               -- 只存带盐哈希，不存原始 IP
    submitter_ua_hash   text,
    claimed_identity    jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- 提交者自称身份，不可信
    text_raw            text,
    media_blobs         jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- 隔离区对象键，非主存储
    blob_sha256         text,
    size_bytes          bigint,
    quota_bucket        text,
    scan_verdict        jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- 扫描结果，fail closed
    status              text        NOT NULL DEFAULT 'pending',
    reviewed_by         bigint      REFERENCES app_user (user_id),
    reviewed_at         timestamptz,
    reject_reason       text,
    promoted_message_id bigint      REFERENCES message (message_id),
    reject_ttl_at       timestamptz,                        -- 拒绝/超期后清理隔离区字节
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT inbox_item_channel_chk CHECK (channel IN ('web', 'telegram_bot', 'email', 'api')),
    CONSTRAINT inbox_item_status_chk
        CHECK (status IN ('pending', 'reviewing', 'promoted', 'rejected', 'expired'))
);

COMMENT ON TABLE inbox_item IS
    'L2 匿名提报隔离区。与 content_item 刻意不共用表：审查前零副作用、拒绝后不留垃圾行（DESIGN §3.1）。'
    ' 媒体字节存放于隔离区前缀（默认 /data/quarantine/inbox/），与内容寻址主库物理分离，拒绝或超期后由清理任务删除。';

CREATE INDEX inbox_item_status_idx ON inbox_item (status, received_at);
-- 同一待审内容不重复占位
CREATE UNIQUE INDEX inbox_item_pending_blob_key
    ON inbox_item (blob_sha256) WHERE status = 'pending' AND blob_sha256 IS NOT NULL;


CREATE TABLE content_item (
    content_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid             uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    message_id       bigint      NOT NULL REFERENCES message (message_id),
    item_type        text        NOT NULL,                  -- text|image|video|audio|link|sticker|file
    ordinal          smallint    NOT NULL DEFAULT 0,        -- 同消息内顺序
    text_content     text,                                  -- 文本项正文或媒体项说明文本
    ocr_text         text,                                  -- 派生
    caption          text,                                  -- 派生（VLM/LLM）
    content_hash     text,                                  -- 文本项规范化后的 SHA-256
    phash            bigint, dhash bigint, ahash bigint,    -- 感知哈希（图片项）
    embedding_id     text,                                  -- 阶段 2 接线
    vector_ref       text,
    canonical_id     bigint      REFERENCES content_item (content_id),
    dup_group_id     bigint,
    dup_distance     smallint,
    nsfw_level       smallint    NOT NULL DEFAULT 0,        -- 0 safe … 4 restricted
    nsfw_score       real,
    sensitive_flags  text[]      NOT NULL DEFAULT '{}',     -- gore|minor_risk|violence|self_harm|politics…
    lang             text,
    char_count       integer,
    byte_size        bigint,
    quality_score    real,
    status           text        NOT NULL DEFAULT 'active',
    submission_tier  smallint    NOT NULL DEFAULT 0,        -- 0=root 1=designated 2=anonymous
    submitted_by     bigint      REFERENCES app_user (user_id),
    inbox_item_id    bigint      REFERENCES inbox_item (inbox_id),  -- 由收件箱提升时留溯源指针
    egress_state     text        NOT NULL DEFAULT 'local_only',
    egress_scope     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    char_bucket      text,                                  -- 未识别角色时的特征归档桶
    first_seen_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    deleted_at       timestamptz,
    CONSTRAINT content_item_type_chk
        CHECK (item_type IN ('text', 'image', 'video', 'audio', 'link', 'sticker', 'file')),
    CONSTRAINT content_item_status_chk
        CHECK (status IN ('active', 'quarantined', 'archived', 'deleted')),
    CONSTRAINT content_item_nsfw_chk   CHECK (nsfw_level BETWEEN 0 AND 4),
    CONSTRAINT content_item_tier_chk   CHECK (submission_tier IN (0, 1, 2)),
    -- 与 DESIGN §3.3 的 egress_state 取值表一致（含 expired）
    CONSTRAINT content_item_egress_chk
        CHECK (egress_state IN ('local_only', 'pending', 'cleared', 'denied', 'expired')),
    CONSTRAINT content_item_message_ordinal_key UNIQUE (message_id, ordinal)
);

COMMENT ON COLUMN content_item.egress_state IS
    'AI 外发资格。骨架与 MVP 阶段没有任何代码消费该列，默认恒为 local_only（DESIGN §3.3、§8.1）。'
    ' 列的存在是为了让「是否曾经把这条内容发给过第三方」将来是可审计的事实，而不是事后补列。';
COMMENT ON COLUMN content_item.char_bucket IS
    '角色识别失败时的特征归档桶，例如 unclassified/by_hair_color/blue；这是正常可检索的分类，不是垃圾堆（DESIGN §4.9）。';

-- 部分唯一索引：排除软删除行，软删后可重新导入同一内容
CREATE UNIQUE INDEX content_item_content_hash_key
    ON content_item (content_hash)
    WHERE content_hash IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX content_item_dup_group_idx   ON content_item (dup_group_id);
CREATE INDEX content_item_nsfw_idx        ON content_item (nsfw_level) WHERE nsfw_level >= 2;
CREATE INDEX content_item_egress_idx      ON content_item (egress_state, nsfw_level);
CREATE INDEX content_item_tier_status_idx ON content_item (submission_tier, status);
CREATE INDEX content_item_char_bucket_idx ON content_item (char_bucket) WHERE char_bucket IS NOT NULL;
CREATE INDEX content_item_message_idx     ON content_item (message_id, ordinal);


CREATE TABLE media (
    media_id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid               uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    content_id         bigint      REFERENCES content_item (content_id),  -- 允许多项复用同一本体
    sha256             char(64)    NOT NULL UNIQUE,
    blake3             text,
    size_bytes         bigint      NOT NULL,
    mime_detected      text,                                -- 魔数嗅探结果，以此为准
    mime_declared      text,                                -- 仅作参考
    extension          text,
    width              integer,
    height             integer,
    duration_ms        integer,
    frame_count        integer,
    is_animated        boolean     NOT NULL DEFAULT false,
    storage_backend_id bigint      REFERENCES storage_backend (backend_id),
    object_key         text        NOT NULL,                -- ab/cd/<sha256>，内容寻址
    storage_class      text        NOT NULL DEFAULT 'hot',
    thumb_key          text,
    preview_key        text,
    webp_key           text,
    exif               jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- 已清洗敏感字段
    scan_status        text        NOT NULL DEFAULT 'pending',
    scan_engine        text,
    scan_at            timestamptz,
    quarantine_key     text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    deleted_at         timestamptz,
    CONSTRAINT media_storage_class_chk CHECK (storage_class IN ('hot', 'cold', 'archive')),
    CONSTRAINT media_scan_status_chk
        CHECK (scan_status IN ('pending', 'clean', 'infected', 'suspicious', 'skipped')),
    CONSTRAINT media_backend_object_key UNIQUE (storage_backend_id, object_key)
);

COMMENT ON TABLE media IS
    '媒体元数据与指针；字节只在存储后端。删除元数据不删本体（除非显式硬删），'
    '两者由后台一致性任务对账（DESIGN §3.6）。';

CREATE INDEX media_content_idx  ON media (content_id);
CREATE INDEX media_scan_idx     ON media (scan_status) WHERE scan_status <> 'clean';


CREATE TABLE link (
    link_id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id      bigint      REFERENCES content_item (content_id),
    url_raw         text        NOT NULL,
    url_normalized  text,
    url_canonical   text,
    domain          text,
    scheme          text,
    final_url       text,
    redirect_chain  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    fetch_status    text,
    http_status     integer,
    content_type    text,
    fetched_at      timestamptz,
    size_bytes      bigint,
    title           text,
    description     text,
    site_name       text,
    og_image_media_id bigint    REFERENCES media (media_id),
    snapshot_key    text,
    risk_level      smallint    NOT NULL DEFAULT 0,          -- 0 未知 … 3 高危
    risk_reasons    text[]      NOT NULL DEFAULT '{}',
    blocked_reason  text,
    expires_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    CONSTRAINT link_risk_chk CHECK (risk_level BETWEEN 0 AND 3)
);

COMMENT ON TABLE link IS '链接默认只记录不抓取；抓取走沙箱抓取器（DESIGN §4.3、§6.4）';

CREATE INDEX link_content_idx ON link (content_id);
CREATE INDEX link_domain_idx  ON link (domain);


-- ===========================================================================
-- 三、标签体系与模型运行（DESIGN §3.2、§4.8）
-- ===========================================================================

CREATE TABLE tag (
    tag_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid         uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    name         text        NOT NULL,
    name_norm    text        NOT NULL UNIQUE,               -- 小写、全半角、下划线统一
    display_name text,
    namespace    text        NOT NULL,                      -- 见 §4.8 的十个命名空间
    kind         text        NOT NULL DEFAULT 'auto',
    parent_id    bigint      REFERENCES tag (tag_id),
    description  text,
    aliases      text[]      NOT NULL DEFAULT '{}',
    external_ref jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- danbooru/gelbooru tag id 等
    usage_count  integer     NOT NULL DEFAULT 0,
    status       text        NOT NULL DEFAULT 'active',
    merged_into  bigint      REFERENCES tag (tag_id),
    created_by   bigint      REFERENCES app_user (user_id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tag_namespace_chk
        CHECK (namespace IN ('character', 'feature', 'unclassified', 'work', 'artist',
                             'general', 'meta', 'rating', 'source', 'system')),
    CONSTRAINT tag_kind_chk   CHECK (kind IN ('auto', 'human', 'system', 'suggested')),
    CONSTRAINT tag_status_chk CHECK (status IN ('active', 'pending', 'merged', 'deprecated')),
    -- 合并后必须留下指向
    CONSTRAINT tag_merged_needs_target CHECK (status <> 'merged' OR merged_into IS NOT NULL)
);

CREATE INDEX tag_namespace_idx ON tag (namespace) WHERE status = 'active';


CREATE TABLE tag_synonym (
    synonym_id bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tag_id     bigint      NOT NULL REFERENCES tag (tag_id),
    alias      text        NOT NULL,
    alias_norm text        NOT NULL UNIQUE,
    source     text,
    confidence real,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE tag_relation (
    rel_id    bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_tag  bigint      NOT NULL REFERENCES tag (tag_id),
    to_tag    bigint      NOT NULL REFERENCES tag (tag_id),
    relation  text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tag_relation_kind_chk
        CHECK (relation IN ('parent', 'child', 'related', 'conflicts_with')),
    CONSTRAINT tag_relation_key UNIQUE (from_tag, to_tag, relation)
);


CREATE TABLE model_version (
    model_version_id  bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    capability        text        NOT NULL,
    provider          text,
    model_name        text        NOT NULL,
    revision          text,
    params            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    prompt_version    text,
    license           text,
    is_local          boolean     NOT NULL DEFAULT true,
    cost_in_per_mtok  numeric(12, 4),
    cost_out_per_mtok numeric(12, 4),
    active            boolean     NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT model_version_capability_chk
        CHECK (capability IN ('tagger', 'ocr', 'nsfw', 'embed_image', 'embed_text',
                              'llm', 'vlm', 'manual')),
    CONSTRAINT model_version_revision_key UNIQUE (capability, model_name, revision, prompt_version)
);

COMMENT ON TABLE model_version IS
    '所有模型输出必须带 model_version 与 prompt_version，全库重算才是一条 SQL 加一个任务批次（DESIGN §0 判断 4）';


CREATE TABLE classification_run (
    run_id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id       bigint      NOT NULL REFERENCES content_item (content_id),
    capability       text        NOT NULL,
    model_version_id bigint      REFERENCES model_version (model_version_id),
    input_ref        text,
    output           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    confidence       real,
    latency_ms       integer,
    cost_estimate    numeric(12, 6),
    status           text        NOT NULL DEFAULT 'succeeded',
    error_class      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT classification_run_status_chk
        CHECK (status IN ('succeeded', 'failed', 'skipped'))
);

COMMENT ON TABLE classification_run IS
    '「当前标签是怎么来的」完全可追溯，也让「用新模型重跑哪些内容」变成一次查询（DESIGN §3.2）';

CREATE INDEX classification_run_lookup_idx ON classification_run (capability, model_version_id, created_at);
CREATE INDEX classification_run_content_idx ON classification_run (content_id);


CREATE TABLE tag_assignment (
    assign_id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id       bigint      NOT NULL REFERENCES content_item (content_id),
    tag_id           bigint      NOT NULL REFERENCES tag (tag_id),
    source           text        NOT NULL,                  -- auto|human|system|suggested|merged
    confidence       real,
    model_version_id bigint      REFERENCES model_version (model_version_id),
    evidence         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    assigned_by      bigint      REFERENCES app_user (user_id),
    assigned_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at       timestamptz,                           -- 人工否决即负样本，进评估集
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tag_assignment_source_chk
        CHECK (source IN ('auto', 'human', 'system', 'suggested', 'merged')),
    -- 自动判定必须可追溯到模型与置信度（DESIGN §4.6 落地约束）
    CONSTRAINT tag_assignment_auto_needs_model
        CHECK (source <> 'auto' OR (model_version_id IS NOT NULL AND confidence IS NOT NULL)),
    CONSTRAINT tag_assignment_key UNIQUE (content_id, tag_id, source)
);

COMMENT ON TABLE tag_assignment IS
    'suggested 是 LLM 建议的落点：不进检索、不进统计、不参与任何自动决策（DESIGN §4.7.1）';

CREATE INDEX tag_assignment_tag_idx ON tag_assignment (tag_id, content_id) WHERE revoked_at IS NULL;
CREATE INDEX tag_assignment_content_idx ON tag_assignment (content_id) WHERE revoked_at IS NULL;


-- ===========================================================================
-- 四、任务队列、复核、审计（DESIGN §2.3、§3.3、§6.1）
-- ===========================================================================

CREATE TABLE job (
    job_id       bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type     text        NOT NULL,
    target_type  text        NOT NULL,
    target_id    bigint      NOT NULL,
    status       text        NOT NULL DEFAULT 'queued',
    priority     smallint    NOT NULL DEFAULT 100,          -- 数字越小越优先
    attempts     integer     NOT NULL DEFAULT 0,
    max_attempts integer     NOT NULL DEFAULT 5,
    run_after    timestamptz NOT NULL DEFAULT now(),
    locked_by    text,
    locked_at    timestamptz,
    idem_key     text,                                      -- job_type:target_type:target_id:model_version
    last_error   text,
    dead_reason  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    CONSTRAINT job_status_chk
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'dead', 'canceled'))
);

COMMENT ON TABLE job IS
    '用数据库当队列：任务表 + FOR UPDATE SKIP LOCKED + 状态机 + 死信，零额外运维（DESIGN §2.3）。'
    ' 取任务示例：'
    '   UPDATE job SET status=''running'', locked_by=$1, locked_at=now(), attempts=attempts+1'
    '   WHERE job_id = (SELECT job_id FROM job'
    '                   WHERE status=''queued'' AND run_after <= now()'
    '                   ORDER BY priority, run_after'
    '                   FOR UPDATE SKIP LOCKED LIMIT 1)'
    '   RETURNING *;';

-- 幂等第二层：同一目标同一模型版本只处理一次
CREATE UNIQUE INDEX job_idem_key ON job (idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX job_claim_idx ON job (status, priority, run_after) WHERE status IN ('queued', 'failed');
CREATE INDEX job_dead_idx  ON job (status) WHERE status = 'dead';


CREATE TABLE review_task (
    task_id     bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uuid        uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    kind        text        NOT NULL,
    target_type text        NOT NULL,
    target_id   bigint      NOT NULL,
    priority    smallint    NOT NULL DEFAULT 100,
    payload     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status      text        NOT NULL DEFAULT 'pending',
    assigned_to bigint      REFERENCES app_user (user_id),
    resolution  jsonb,
    resolved_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT review_task_kind_chk
        CHECK (kind IN ('inbox_submission', 'egress_review', 'low_confidence', 'nsfw_uncertain',
                        'dedup_conflict', 'scan_gray', 'tag_conflict', 'link_risk', 'char_ambiguous')),
    CONSTRAINT review_task_status_chk
        CHECK (status IN ('pending', 'in_progress', 'resolved', 'canceled'))
);

COMMENT ON TABLE review_task IS
    '三类队列在 UI 上必须分开呈现：提报审查 / 外发放行 / 质量纠错，决策依据与风险完全不同（DESIGN §2.3）';

CREATE INDEX review_task_queue_idx ON review_task (kind, status, priority, created_at);
CREATE INDEX review_task_target_idx ON review_task (target_type, target_id);


CREATE TABLE audit_log (
    audit_id   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_type text        NOT NULL,                        -- user|system|connector|llm
    actor_id   bigint,
    action     text        NOT NULL,
    target_type text,
    target_id  bigint,
    before_state jsonb,                                     -- DESIGN 写作 before（偏差[2]）
    after_state  jsonb,                                     -- DESIGN 写作 after（偏差[2]）
    result     text,
    ip         inet,
    user_agent text,
    request_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT audit_log_actor_chk CHECK (actor_type IN ('user', 'system', 'connector', 'llm'))
);

COMMENT ON TABLE audit_log IS
    '所有特权操作、授权变更、越权尝试都写这里；外发相关记录在阶段 2 另有 egress_log（DESIGN §6.1、§6.3）';

CREATE INDEX audit_log_target_idx ON audit_log (target_type, target_id, created_at DESC);
CREATE INDEX audit_log_actor_idx  ON audit_log (actor_type, actor_id, created_at DESC);
CREATE INDEX audit_log_action_idx ON audit_log (action, created_at DESC);


-- ===========================================================================
-- 五、设置层（DESIGN §6.2）
-- ===========================================================================

CREATE TABLE setting (
    key               text        PRIMARY KEY,
    value             jsonb,
    value_type        text        NOT NULL,                 -- 与注册表 type 对应
    source            text        NOT NULL DEFAULT 'default',
    schema_version    integer     NOT NULL DEFAULT 1,
    updated_by        bigint      REFERENCES app_user (user_id),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    is_dangerous      boolean     NOT NULL DEFAULT false,
    last_validated_at timestamptz,
    CONSTRAINT setting_source_chk CHECK (source IN ('default', 'yaml', 'db', 'env'))
);

COMMENT ON TABLE setting IS
    'L1 设置页的落地：source 让「这个值从哪来」可被查询，对应 pc config show --explain（DESIGN §6.2.4）。'
    ' 设置项的自描述（label/help/impact/range）不在数据库里，在版本化的 config/settings-registry.yaml 中。';


CREATE TABLE setting_change (
    change_id      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key            text        NOT NULL,
    before_state   jsonb,                                   -- DESIGN 写作 before（偏差[2]）
    after_state    jsonb,                                   -- DESIGN 写作 after（偏差[2]）
    changed_by     bigint      REFERENCES app_user (user_id),
    changed_via    text        NOT NULL,                    -- ui|yaml|env|migration
    precheck_result jsonb      NOT NULL DEFAULT '{}'::jsonb,
    applied        boolean     NOT NULL DEFAULT false,
    rolled_back    boolean     NOT NULL DEFAULT false,
    applied_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT setting_change_via_chk CHECK (changed_via IN ('ui', 'yaml', 'env', 'migration'))
);

COMMENT ON TABLE setting_change IS
    '每次配置改动的前后值与校验结果可追溯；失败必须回滚而不是把系统卡在不可用状态（DESIGN §6.2.2）';

CREATE INDEX setting_change_key_idx ON setting_change (key, created_at DESC);


-- ===========================================================================
-- 六、容量管理与保留策略（DESIGN §7.5）
-- ===========================================================================

CREATE TABLE retention_policy (
    policy_id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name               text        NOT NULL UNIQUE,
    scope_type         text        NOT NULL,
    scope_id           bigint,
    strategy           text        NOT NULL,
    retain_days        integer,
    min_keep_count     integer,                             -- 条数下限，保证不删空
    max_total_bytes    bigint,
    max_items          integer,
    age_tiebreak       text,                                -- 超限时先删什么
    never_evict_tags   text[]      NOT NULL DEFAULT '{}',
    never_evict_nsfw_max smallint,
    action             text        NOT NULL,
    apply_to           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    dry_run            boolean     NOT NULL DEFAULT true,    -- 默认 dry-run，安全阀
    enabled            boolean     NOT NULL DEFAULT true,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT retention_scope_chk
        CHECK (scope_type IN ('global', 'source', 'tag', 'nsfw_level', 'submission_tier')),
    CONSTRAINT retention_strategy_chk
        CHECK (strategy IN ('keep_forever', 'ttl', 'rolling_cap', 'size_budget', 'hot_cold_tier')),
    CONSTRAINT retention_tiebreak_chk
        CHECK (age_tiebreak IS NULL OR age_tiebreak IN
               ('oldest', 'least_accessed', 'lowest_quality', 'not_in_any_collection')),
    CONSTRAINT retention_action_chk
        CHECK (action IN ('archive', 'soft_delete', 'hard_delete', 'demote_storage_class'))
);

COMMENT ON TABLE retention_policy IS
    '滚动删除即最大的成本控制手段。dry_run 默认 true：先出报告，确认后才执行（DESIGN §7.5）';


CREATE TABLE storage_usage (
    snapshot_at  timestamptz NOT NULL,
    backend_id   bigint      NOT NULL REFERENCES storage_backend (backend_id),
    class        text        NOT NULL,
    object_count bigint      NOT NULL DEFAULT 0,
    total_bytes  bigint      NOT NULL DEFAULT 0,
    item_count   bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_at, backend_id, class),
    CONSTRAINT storage_usage_class_chk CHECK (class IN ('hot', 'cold', 'archive'))
);

COMMENT ON TABLE storage_usage IS
    '时序快照，驱动水位与「按当前增速还有多少天到水位线」的预测（DESIGN §7.5）';


CREATE TABLE retention_run (
    run_id        bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    policy_id     bigint      REFERENCES retention_policy (policy_id),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    dry_run       boolean     NOT NULL DEFAULT true,
    candidates    integer,
    deleted_media integer,
    freed_bytes   bigint,
    blocked_count integer,
    report        jsonb       NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE retention_run IS
    'dry-run 报告与实际执行结果必须可对比：验收标准要求两者一致（DESIGN §9 阶段 1 验收⑨）';

CREATE INDEX retention_run_policy_idx ON retention_run (policy_id, started_at DESC);


-- ===========================================================================
-- 迁移记录
-- ===========================================================================
INSERT INTO schema_migration (version, name, note)
VALUES (1, '0001_init', '核心数据模型首版；未包含 content_embedding / ai_provider_policy / egress_log')
ON CONFLICT (version) DO NOTHING;

-- ===========================================================================
-- 后续迁移（占位，勿在此文件继续追加）：
--   0002_embedding.sql        content_embedding + pgvector（DESIGN §5.3，阶段 2）
--   0003_egress.sql           ai_provider_policy + egress_log（DESIGN §6.3，阶段 2）
--   0004_search_document.sql  content_item.search_document 物化列与权重（DESIGN §5.1）
-- ===========================================================================
