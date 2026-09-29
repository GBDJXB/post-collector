# migrations —— 建表脚本

## 目录结构

```
migrations/
├── sqlite/
│   └── 0001_init.sql      ← **权威**：当前使用的 schema（SQLite 方言）
├── postgres/
│   └── 0001_init.sql      ← 历史：PostgreSQL 版，已冻结，随 PG 路径一起退役
└── README.md              本文件
```

## 为什么有两套

项目最初用 PostgreSQL，v3 起换成 SQLite 单文件（DESIGN §3.2、§3.4）。两版**逻辑
schema 是同一套**（表名、列名、约束一一对应），差别只在方言：

| 维度 | PostgreSQL 版 | SQLite 版 |
| --- | --- | --- |
| 主键 | `bigint GENERATED ALWAYS AS IDENTITY` | `INTEGER PRIMARY KEY`（rowid 别名） |
| JSON | `jsonb` + GIN 索引 | `TEXT` + `json_valid` CHECK（无 GIN） |
| 数组 | `text[]` | `TEXT` 存 JSON 数组 |
| 时间戳 | `timestamptz` + `DEFAULT now()` | `TEXT`（ISO-8601 UTC 毫秒）+ `DEFAULT (strftime(...))` |
| 布尔 | `boolean` | `INTEGER` + `CHECK (col IN (0,1))` |
| UUID | `uuid` + `DEFAULT gen_random_uuid()` | `TEXT`，由**应用层**生成 |
| 全文检索 | `pg_trgm` + `tsvector` | FTS5 双虚拟表（`trigram` / `unicode61`） |
| 类型纪律 | 引擎强类型 | 全部 `STRICT` 表 |
| 注释 | `COMMENT ON` | 文件内 `--` 注释（SQLite 无该语句） |

`postgres/0001_init.sql` 已在 PostgreSQL 16.15 上真实执行成功并冻结，其文件头记录了
当时的执行证据与七条偏差（`user` → `app_user` 等保留字改名）。它保留在这里是为了
可追溯；PG 路径退役时会删除整个目录。**不要再改它。**

## 执行方式

```bash
uv run pc db apply                 # 应用 migrations/sqlite/0001_init.sql
uv run pc db status                # 只读复核：表数、FTS5、PRAGMA、结构指纹
uv run pc db apply --path <文件>   # 指定数据库文件（默认 <data_root>/pc.sqlite3）
```

执行由 `pc.adapters.sqlite.apply_migration` 负责，语义是：

- **整篇在单个显式事务内执行**（`BEGIN IMMEDIATE` … `COMMIT`），失败整体回滚，
  绝不留下半成品。SQLite 的 DDL 是事务性的，这一点靠它成立。
- 语句切分用 `sqlite3.complete_statement()`，**不要**用 `text.split(';')`：
  字符串字面量与注释里都可能出现分号。
- 已应用则短路；结构指纹（`schema_migration.checksum`，剔除整行注释后的 SHA-256）
  与文件不一致时**只报告不覆盖**——漂移要被看见，不该被抹掉。

## 写新迁移的规矩

1. **已应用的版本不可修改**：任何结构改动都以新文件追加（`0002_*.sql`），
   并同步更新 `pc.adapters.sqlite.EXPECTED_TABLES`——否则新表会被 `pc db status`
   报成"计划外的表"，这是刻意的强制提醒。
2. **迁移文件里不得出现 `PRAGMA`**：PRAGMA 基线（`journal_mode`、`foreign_keys`、
   `busy_timeout`、`synchronous`、`temp_store`）由适配器在建立连接时执行。
   `journal_mode` 在事务内无法切换，写进 `.sql` 会直接报错。适配器会拒绝这类语句。
3. **不要写 `BEGIN` / `COMMIT`**：事务由适配器管理。
4. **时间戳默认值与 PG 版逐列对齐**：PG 里带 `DEFAULT now()` 的列，这里写
   `DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))`；PG 里没有默认值的列
   （`deleted_at`、`finished_at`、`snapshot_at`、`expires_at` 等）也一律不给。
   `updated_at` 由应用层在 UPDATE 时重写（不建触发器）。
5. 新增表时**同时**补：`STRICT`、`CHECK` 约束、必要的索引、以及（若涉及检索）
   FTS5 同步触发器。
