"""本机账号管理与受邀身份认证；权限由封闭策略生成。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from uuid import uuid4

from pc.adapters import passwords
from pc.adapters import sqlite as db


def username(value: str) -> str:
    """规范化登录名；创建时另行拒绝保留名称。"""
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,64}", name, flags=re.ASCII):
        raise ValueError("用户名须为 3~64 位 ASCII 字母、数字、点、下划线或连字符")
    return name


def audit(conn, action: str, user_id: int | None = None, *, result="success", local=False):
    conn.execute(
        "INSERT INTO audit_log(actor_type,actor_id,action,target_type,target_id,result,user_agent) "
        "VALUES (?,?,?,?,?,?,?)",
        ("system" if local or user_id is None else "user", None if local else user_id,
         action, "app_user", user_id, result, "local_root" if local else "remote_auth"),
    )


def _public(row):
    return {key: row[key] for key in (
        "user_id", "username", "display_name", "tier", "status", "created_at",
        "updated_at", "disabled_at", "last_login_at",
    )}


def list_users(conn):
    return [_public(row) for row in conn.execute("SELECT * FROM app_user ORDER BY user_id")]


def _hash(password: str) -> str:
    if not 15 <= len(password) <= 128:
        raise ValueError("密码长度须为 15~128 字符")
    return passwords.hash_password(password)


def check_new_username(conn, name: str) -> str:
    """创建前的只读预检；写入时须在事务内再次检查。"""
    name = username(name)
    if name == "root":
        raise ValueError("root 是本机管理身份的保留名称")
    if conn.execute("SELECT 1 FROM app_user WHERE lower(username)=?", (name,)).fetchone():
        raise ValueError("用户名已存在")
    return name


def create_user(conn, name: str, password: str):
    name = check_new_username(conn, name)
    hashed = _hash(password)
    with db.transaction(conn):
        check_new_username(conn, name)
        now = db.now_iso()
        row = conn.execute(
            "INSERT INTO app_user(uuid,username,display_name,tier,password_hash,created_at,updated_at) "
            "VALUES (?,?,?,1,?,?,?) RETURNING *", (str(uuid4()), name, name, hashed, now, now),
        ).fetchone()
        audit(conn, "user.create", row["user_id"], local=True)
    return _public(row)


def change_user(conn, name: str, operation: str, password: str | None = None):
    name = username(name)
    hashed = _hash(password) if operation == "reset-password" and password is not None else None
    if operation not in {"disable", "enable", "reset-password"}:
        raise ValueError("未知账号操作")
    if operation == "reset-password" and hashed is None:
        raise ValueError("需要新密码")
    with db.transaction(conn):
        row = conn.execute("SELECT * FROM app_user WHERE username=?", (name,)).fetchone()
        if row is None or row["tier"] != 1:
            raise ValueError("仅能管理已存在的受邀账号")
        # 每次管理操作推进版本，即使同一毫秒内停用后重新启用，旧会话也无法复活。
        now = db.now_iso()
        if now <= row["updated_at"]:
            instant = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            now = (instant + timedelta(milliseconds=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if operation == "reset-password":
            conn.execute("UPDATE app_user SET password_hash=?,updated_at=? WHERE user_id=?",
                         (hashed, now, row["user_id"]))
        else:
            disabled = operation == "disable"
            conn.execute("UPDATE app_user SET status=?,disabled_at=?,updated_at=? WHERE user_id=?",
                         ("disabled" if disabled else "active", now if disabled else None,
                          now, row["user_id"]))
        audit(conn, "user." + operation, row["user_id"], local=True)
        result = conn.execute("SELECT * FROM app_user WHERE user_id=?", (row["user_id"],)).fetchone()
    return _public(result)


def fingerprint(row) -> str:
    return hashlib.sha256(json.dumps([row["password_hash"], row["updated_at"]]).encode()).hexdigest()


def authenticate(conn, name: str, password: str):
    try:
        name = username(name)
    except ValueError:
        name = ""
    row = conn.execute("SELECT * FROM app_user WHERE username=?", (name,)).fetchone()
    eligible = (row is not None and row["tier"] == 1 and row["status"] == "active"
                and row["disabled_at"] is None and bool(row["password_hash"]))
    valid = passwords.verify_password(row["password_hash"] if eligible else None, password)
    with db.transaction(conn):
        # 校验密码期间可能发生停用或重置，写入登录记录前再次确认。
        fresh = conn.execute("SELECT * FROM app_user WHERE user_id=?", (row["user_id"],)).fetchone() if row else None
        valid = bool(valid and eligible and fresh is not None and fresh["tier"] == 1
                     and fresh["status"] == "active" and fresh["disabled_at"] is None
                     and fingerprint(fresh) == fingerprint(row))
        audit(conn, "auth.login", row["user_id"] if row else None,
              result="success" if valid else "denied")
        if valid:
            conn.execute("UPDATE app_user SET last_login_at=? WHERE user_id=?", (db.now_iso(), row["user_id"]))
    return fresh if valid else None


def active_session_user(conn, user_id: int, version: str):
    row = conn.execute("SELECT * FROM app_user WHERE user_id=?", (user_id,)).fetchone()
    if (row is None or row["tier"] != 1 or row["status"] != "active"
            or row["disabled_at"] is not None or fingerprint(row) != version):
        return None
    return row


def identity(*, user_id=None, name="root", remote=False):
    operations = []
    if remote:
        specs = [
            ("content.search", "pc search <关键词> --type image --type mixed --nsfw-level 0 --json",
             "检索获准范围内的图片消息；混合消息必须含图片。"),
            ("content.detail", "pc show --message <ID> --json", "查看获准图片消息的详情和媒体引用。"),
        ]
    else:
        specs = ROOT_OPERATIONS
    for code, command, comment in specs:
        operations.append({"code": code, "command": command, "comment": comment,
                           "execution_context": "remote" if remote else "local_cli",
                           "executable": not remote,
                           "reason": "当前入口仅实现登录与权限说明。" if remote else "在本机 CLI 执行；此 JSON 不执行命令。"})
    return {"identity_version": 1,
            "identity": {"user_id": user_id, "username": name,
                         "role": "invited" if remote else "root", "entry": "remote" if remote else "local"},
            "scope": {"sources": "all", "content": "safe-images-and-image-containing-messages" if remote else "local-root"},
            "operations": operations}


ROOT_OPERATIONS = [
    ("content.search", "pc search <关键词> --json", "检索归档内容。"),
    ("content.list", "pc item list --json", "列出内容项。"),
    ("content.show", "pc show --message <ID> --json", "读取消息详情。"),
    ("content.open", "pc open --message <ID>", "使用本机默认程序打开内容。"),
    ("content.reveal", "pc reveal --message <ID>", "在本机文件管理器定位内容。"),
    ("content.upload", "pc upload <路径>", "上传本地内容并处理。"),
    ("content.import", "pc import <路径> --source <来源>", "从已授权本地来源导入。"),
    ("jobs.work", "pc work", "处理排队任务。"),
    ("tags.add", "pc tag add --message <ID> --tag <标签>", "添加人工标签。"),
    ("tags.remove", "pc tag remove --message <ID> --tag <标签>", "撤销人工标签。"),
    ("tags.list", "pc tag list --json", "列出有效标签。"),
    ("tags.show", "pc tag show --message <ID> --json", "查看消息标签关联。"),
    ("tags.confirm", "pc tag confirm <ID>", "确认标签建议。"),
    ("tags.reject", "pc tag reject <ID>", "拒绝标签建议。"),
    ("aliases.add", "pc tag alias add --tag <标签> --alias <别名>", "添加别名。"),
    ("aliases.remove", "pc tag alias remove --alias <别名>", "移除别名。"),
    ("aliases.list", "pc tag alias list --tag <标签> --json", "查看别名。"),
    ("sources.list", "pc source list --json", "列出来源。"),
    ("sources.add", "pc source add <来源> --kind manual", "创建本地来源。"),
    ("config.show", "pc config show --explain", "查看生效配置及来源。"),
    ("config.seed", "pc config seed", "播种缺失设置。"),
    ("db.apply", "pc db apply", "应用迁移。"),
    ("db.status", "pc db status", "检查库结构。"),
    ("db.backup", "pc db backup", "创建数据库备份。"),
    ("db.reindex", "pc db reindex --recompute-derived", "重算派生数据及索引。"),
    ("system.doctor", "pc doctor", "检查运行环境。"),
    ("system.serve", "pc serve", "启动本机管理入口。"),
    ("render.table", "pc render table", "从标准输入渲染查询 JSON。"),
    ("users.create", "pc user create <用户名>", "创建受邀账号并隐藏输入密码。"),
    ("users.list", "pc user list --json", "列出账号状态。"),
    ("users.disable", "pc user disable <用户名>", "停用账号并使会话失效。"),
    ("users.enable", "pc user enable <用户名>", "启用账号；旧会话保持失效。"),
    ("users.reset", "pc user reset-password <用户名>", "重置密码并使旧会话失效。"),
]
