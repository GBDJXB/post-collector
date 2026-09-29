# migrations —— 建表脚本

## 目录结构

```
migrations/
├── sqlite/
│   ├── 0001_init.sql                      ← 基线：34 张表 + 2 个 FTS5 虚拟表 + 6 个触发器
│   └── 0002_content_hash_not_unique.sql   ← 文本的精确重复改为"归组"语义（见下）
└── README.md                              本文件
```

**整个目录按文件名序号顺序应用**，不是只应用某一个文件。

## 历史：PostgreSQL 版去哪了

项目最初用 PostgreSQL，v3 起换成 SQLite 单文件（DESIGN §3.2、§3.4）。迁移完成后，
PostgreSQL 版建表脚本（`migrations/postgres/0001_init.sql`）**已随 PG 路径一起删除**：

- 它的执行证据与七条偏差记录（`user` → `app_user`、`authorization` →
  `source_authorization` 等为避开保留字而做的改名）保留在 git 历史里，
  也在 `sqlite/0001_init.sql` 的文件头有摘要；
- 数据库里没有任何业务数据（迁移前实测：只有 `schema_migration` 1 行、`setting` 6 行），
  所以**不存在数据搬迁**——那 32 张表在 SQLite 侧重建成 34 张（新增 `content_media`
  与 `hash_tombstone`）。

两版方言的对照关系（保留在这里，方便读旧提交或旧 issue 时换算）：

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

## 执行方式

```bash
uv run pc db apply                 # 应用 migrations/sqlite/0001_init.sql
uv run pc db status                # 只读复核：表数、FTS5、PRAGMA、结构指纹
uv run pc db apply --path <文件>   # 指定数据库文件（默认 <data_root>/pc.sqlite3）
```

执行由 `pc.adapters.sqlite.apply_migration` 负责，语义是：

- **每个文件在自己的显式事务内执行**（`BEGIN IMMEDIATE` … `COMMIT`），失败只回滚
  它自己，**已成功的版本保持已应用**。这是"一个文件一个事务"的取舍：比"整个目录一个
  大事务"更容易恢复，也不会因为第 3 个文件写错而回退掉前两个。SQLite 的 DDL 是
  事务性的，这一点靠它成立。
- 只应用**尚未记账**的版本；已应用的版本不重复执行。记账写进
  `schema_migration(version, name, checksum, applied_at)`。
- 语句切分用 `sqlite3.complete_statement()`，**不要**用 `text.split(';')`：
  字符串字面量与注释里都可能出现分号。
- 结构指纹（`checksum`，剔除整行注释后的 SHA-256）**逐版本**比对，与文件不一致时
  **只报告不覆盖**——漂移要被看见，不该被抹掉。
- 记账表 `schema_migration` 由**适配器兜底创建**（迁移文件里也有一份带
  `IF NOT EXISTS` 的定义，等价）。它属于运行器的基础设施，不计入"业务表"，所以
  "只有它没有别的表"仍然是干净库。

## 文件名的规矩

- 形如 `<4 位序号>_<描述>.sql`，序号就是版本号；
- **序号必须从 1 连续**：`0001`、`0002`…。断号会让"哪些已应用"变得不可回答，
  适配器会**直接报错**而不是猜；
- 同一序号只能有一个文件；非 `.sql` 文件被忽略。

## 写新迁移的规矩

1. **已应用的版本不可修改**：任何结构改动都以新文件追加（下一个序号），
   并同步更新 `pc.adapters.sqlite.EXPECTED_TABLES`——否则新表会被 `pc db status`
   报成"计划外的表"，这是刻意的强制提醒。**改已应用的文件会让结构指纹漂移**，
   `pc db status` 会报出来（它只报告、不覆盖）。
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
6. 改了索引/唯一性时，**同步改 `tests/test_schema_integration.py` 的结构断言**：
   那里是红线看守，它必须跟着事实走，而不是跟着旧文件走。

## 0002 为什么把 `content_hash` 的唯一性去掉

`content_item.message_id` 是 NOT NULL（一个内容项必属于某条消息），所以"同一段文本
出现在两条消息里"在**唯一**索引下根本无法表示——第二条会被直接拒绝。这与 v3.1 修掉的
`media` 多对多缺陷是同一类问题。0002 把该索引降级为普通部分索引，重复的表示交给
DESIGN §3.6/§3.8 已经设计好的 canonical 归组（`dup_group_id` + `canonical_id`，由导入
层填写）。**`media.sha256` 仍然是全局唯一**——字节层面"同一份内容只有一行 media"是
硬约束，它保证不会重复占盘。
