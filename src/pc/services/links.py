"""链接作为归档文本处理：只解析明确的 HTTP(S) URL，不访问网络。"""

from __future__ import annotations

import ipaddress
import re
import unicodedata

_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")


def normalize_url(raw: str) -> str:
    """只规范化协议/域名/默认端口；路径及查询参数的含义不做猜测。"""
    if not isinstance(raw, str):
        raise ValueError("URL 须为文本")
    value = raw.strip()
    if not value or len(value) > 2048 or any(c.isspace() or unicodedata.category(c) in {"Cc", "Cf"} for c in value):
        raise ValueError("URL 为空、过长或含空白/控制字符")
    if _BAD_PERCENT.search(value):
        raise ValueError("URL 包含无效百分号转义")
    if not re.match(r"^https?://", value, re.IGNORECASE):
        raise ValueError("只接受明确的 http:// 或 https:// URL")
    parsed = re.fullmatch(r"(?i)(https?)://([^/?#]+)([^?#]*)(?:\?([^#]*))?(?:#(.*))?", value)
    if parsed is None:
        raise ValueError("URL 主机无效")
    scheme, authority, path, query, fragment = parsed.groups()
    if "@" in authority or "\\" in authority or authority.startswith(":"):
        raise ValueError("URL 不能包含凭据且必须有主机")
    if authority.startswith("["):
        host, separator, suffix = authority.partition("]")
        if not separator:
            raise ValueError("IPv6 主机无效")
        try:
            host = f"[{ipaddress.IPv6Address(host[1:]).compressed}]"
        except ipaddress.AddressValueError as exc:
            raise ValueError("IPv6 主机无效") from exc
        if suffix and not suffix.startswith(":"):
            raise ValueError("端口无效")
        port_text = suffix[1:] if suffix.startswith(":") else ""
        if suffix == ":":
            raise ValueError("端口无效")
    else:
        host, separator, port_text = authority.partition(":")
        if ":" in port_text:
            raise ValueError("端口无效")
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("URL 主机无效") from exc
        if not host or any(char in host for char in "/\\?#@%"):
            raise ValueError("URL 主机无效")
    port = None
    if port_text or (authority.endswith(":") and not authority.startswith("[")):
        if not port_text.isdecimal() or not 0 < int(port_text) <= 65535:
            raise ValueError("端口无效")
        port = int(port_text)
    lower_scheme = scheme.lower()
    normalized_authority = host + (f":{port}" if port and (lower_scheme, port) not in {("http", 80), ("https", 443)} else "")
    return f"{lower_scheme}://{normalized_authority}{path or '/'}" + (f"?{query}" if query is not None else "") + (f"#{fragment}" if fragment is not None else "")
