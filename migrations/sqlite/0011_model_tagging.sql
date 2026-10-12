-- pc:foreign_keys=off
-- 内容/独立组合共用分类记录，保留旧行与索引。
CREATE TABLE classification_run_rebuild (
    run_id INTEGER PRIMARY KEY,
    content_id INTEGER REFERENCES content_item(content_id),
    capability TEXT NOT NULL,
    model_version_id INTEGER REFERENCES model_version(model_version_id),
    input_ref TEXT,
    output TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    latency_ms INTEGER,
    cost_estimate REAL,
    status TEXT NOT NULL DEFAULT 'succeeded',
    error_class TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    composition_id INTEGER REFERENCES composition(composition_id),
    CONSTRAINT classification_run_target_chk CHECK ((content_id IS NOT NULL) <> (composition_id IS NOT NULL)),
    CONSTRAINT classification_run_status_chk CHECK(status IN ('succeeded','failed','skipped')),
    CONSTRAINT classification_run_output_json_chk CHECK(json_valid(output))
) STRICT;
INSERT INTO classification_run_rebuild(run_id,content_id,capability,model_version_id,input_ref,output,confidence,latency_ms,cost_estimate,status,error_class,created_at,composition_id)
SELECT run_id,content_id,capability,model_version_id,input_ref,output,confidence,latency_ms,cost_estimate,status,error_class,created_at,NULL FROM classification_run;
DROP TABLE classification_run;
ALTER TABLE classification_run_rebuild RENAME TO classification_run;
CREATE INDEX classification_run_lookup_idx ON classification_run(capability,model_version_id,created_at);
CREATE INDEX classification_run_content_idx ON classification_run(content_id);
CREATE INDEX classification_run_composition_idx ON classification_run(composition_id);

CREATE TABLE composition_tag_assignment_rebuild (
    assign_id INTEGER PRIMARY KEY,
    composition_id INTEGER NOT NULL REFERENCES composition(composition_id),
    tag_id INTEGER NOT NULL REFERENCES tag(tag_id),
    source TEXT NOT NULL DEFAULT 'human' CHECK(source IN ('human','suggested')),
    assigned_by INTEGER REFERENCES app_user(user_id),
    assigned_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    confidence REAL CHECK(confidence IS NULL OR confidence BETWEEN 0 AND 1),
    model_version_id INTEGER REFERENCES model_version(model_version_id),
    evidence TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(evidence)),
    CHECK(source <> 'suggested' OR (model_version_id IS NOT NULL AND confidence IS NOT NULL)),
    UNIQUE(composition_id,tag_id,source)
) STRICT;
INSERT INTO composition_tag_assignment_rebuild(assign_id,composition_id,tag_id,source,assigned_by,assigned_at,updated_at,revoked_at,confidence,model_version_id,evidence)
SELECT assign_id,composition_id,tag_id,source,assigned_by,assigned_at,updated_at,revoked_at,NULL,NULL,'{}' FROM composition_tag_assignment;
DROP TABLE composition_tag_assignment;
ALTER TABLE composition_tag_assignment_rebuild RENAME TO composition_tag_assignment;
CREATE INDEX composition_tag_active_idx ON composition_tag_assignment(tag_id,composition_id) WHERE revoked_at IS NULL;

-- 外发证据不保存密钥、内容、原始响应或异常。
CREATE TABLE egress_log (
    request_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    content_id INTEGER REFERENCES content_item(content_id),
    composition_id INTEGER REFERENCES composition(composition_id),
    purpose TEXT NOT NULL CHECK(purpose='tagging'),
    provider TEXT NOT NULL CHECK(provider='deepseek'),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    catalog_digest TEXT NOT NULL,
    authorization_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    text_bytes INTEGER NOT NULL CHECK(text_bytes>=0),
    image_bytes INTEGER NOT NULL CHECK(image_bytes>=0),
    image_count INTEGER NOT NULL CHECK(image_count>=0),
    candidate_count INTEGER NOT NULL CHECK(candidate_count BETWEEN 1 AND 500),
    request_bytes INTEGER NOT NULL CHECK(request_bytes>=0),
    status TEXT NOT NULL CHECK(status IN ('prepared','dispatching','succeeded','failed','outcome_unknown','stale')),
    error_class TEXT CHECK(error_class IS NULL OR error_class IN ('provider_auth','provider_limit','provider_failure','invalid_output','timeout','network','write_failure','stale')),
    provider_request_id TEXT,
    usage TEXT CHECK(usage IS NULL OR json_valid(usage)),
    run_id INTEGER REFERENCES classification_run(run_id),
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    finished_at TEXT,
    CHECK((content_id IS NOT NULL) <> (composition_id IS NOT NULL))
) STRICT;
CREATE INDEX egress_log_budget_idx ON egress_log(dispatched_at) WHERE dispatched_at IS NOT NULL;
