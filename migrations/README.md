# migrations —— 建表脚本

## 目录结构

```
migrations/
├── sqlite/
│   ├── 0001_init.sql                      ← 基线：34 张表 + 2 个 FTS5 虚拟表 + 6 个触发器
│   ├── 0002_content_hash_not_unique.sql   ← 文本的精确重复改为"归组"语义（见下）
│   ├── 0003_message_media_refs.sql        ← 媒体引用落进信封，消息才可重放
│   ├── 0004_manual_source_and_origin_note.sql  ← 重建两张表放宽 kind/mode 枚举 + 备注列
│   ├── 0005_search_names_and_time.sql     ← 文件名列 + 消息时间索引
│   ├── 0006_sent_at_on_content_item.sql   ← 消息时间落到内容项，改单表索引（跨表 ORDER BY 用不上索引）
│   ├── 0007_post_table.sql                ← post 派生表：一帖一行，检索的结果单位（第 35 张表）
│   ├── 0008_search_visibility_indexes.sql ← 帖可见性与候选映射的覆盖索引，不改表结构
│   ├── 0009_message_visibility_index.sql  ← 消息可见性与来源的部分索引，减少信封回读
│   ├── 0010_compositions.sql              独立组合、有序成员及组合标签；使旧帖签名失效
└── README.md                              本文件
```

**整个目录按文件名序号顺序应用**，不是只应用某一个文件。

**迁移里可以带数据语句**：0007 就是"建派生表 + 立刻回填"的例子。派生数据（帖行）不回填的话，
升级之后检索会返回空——那是最糟的失败模式；而回填是确定性的纯计算，与建表在同一个事务里，
失败整体回滚。业务数据的搬迁不在此列（那要单独设计）。

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
7. 放宽 `CHECK` 枚举（例如给某个取值集合加一项）在 SQLite 里**做不到就地修改**，
   只能重建表，做法见下一节。

## 重建表（放宽 CHECK 枚举的唯一办法）

SQLite 没有 `ALTER TABLE DROP CONSTRAINT`。要改一个 `CHECK`，只能：建临时表 →
`INSERT ... SELECT *` → `DROP TABLE` 原表 → `ALTER TABLE ... RENAME TO` 原表名 →
**重建索引与触发器**（它们跟着旧表一起没了）。

`DROP TABLE` 在 `foreign_keys=ON` 时会做隐式 DELETE 并被子表的外键挡下，而
`PRAGMA foreign_keys` 在事务内是空操作——所以这种迁移必须在文件里声明一条指令：

```sql
-- pc:foreign_keys=off
```

迁移器看到它就会：在 `BEGIN` **之前**关掉外键 → 照常在一个事务里执行 → **提交前**跑
`PRAGMA foreign_key_check`（有违例就整体回滚并报出来）→ 结束后恢复 `foreign_keys=ON`。
指令是注释，不参与结构指纹。**未知指令一律报错**：打错一个字母却静默失效，会让重建
在开着外键的情况下跑，报出来的错与真正的原因差着十万八千里。

约定与注意：

- 临时表名一律以 **`_rebuild`** 结尾（`source_rebuild`）。表清单校验会忽略这个后缀，
  另一条断言保证库里不会留下没改名的临时表。
- `INSERT ... SELECT *` 依赖**列顺序完全一致**：临时表的 DDL 必须逐字复制原表，
  只改那一处要改的 `CHECK`。`test_schema_integration` 里有一条断言把重建后的定义与
  0001 的定义做逐字比对（只允许枚举那一处不同），手抄抄漏一列会被它抓住。
- **不需要 `legacy_alter_table`**（本项目在 SQLite 3.50.4 上实测：开关两种设置都成功，
  子表外键照样指向重建后的表）。外键子句只在运行时解析，指向一张暂时不存在的表不会让
  `ALTER TABLE RENAME` 的 schema 重解析失败。真正需要它的是**视图与触发器**里引用旧
  表名——本项目的这两张表上没有。万一将来某个重建牵扯到视图/触发器而报
  `error in table ... after rename`，再来给指令加一个取值。

## 0002 为什么把 `content_hash` 的唯一性去掉

`content_item.message_id` 是 NOT NULL（一个内容项必属于某条消息），所以"同一段文本
出现在两条消息里"在**唯一**索引下根本无法表示——第二条会被直接拒绝。这与 v3.1 修掉的
`media` 多对多缺陷是同一类问题。0002 把该索引降级为普通部分索引，重复的表示交给
DESIGN §3.6/§3.8 已经设计好的 canonical 归组（`dup_group_id` + `canonical_id`，由导入
层填写）。**`media.sha256` 仍然是全局唯一**——字节层面"同一份内容只有一行 media"是
硬约束，它保证不会重复占盘。

0010 将旧 content_signature/canonical_post_id/dup_group_id 置空，不改原始消息、内容或媒体。升级后运行 pc db reindex --recompute-derived 恢复 v2 有序签名；重算前消息均独立显示。组合成员允许重复，通过位置区分，组合删除不级联删除原内容。
