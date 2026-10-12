"""唯一模型 HTTP 传输位置；只接受网关已确认并落账的请求字节。"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import httpx

from pc.domain import ClassificationResult, TagCandidate, TagNamespace

ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
PROMPT_VERSION = "tagging-v1"
RESPONSE_LIMIT = 256 * 1024


class ModelError(ValueError):
    """只携带稳定错误分类，不带 HTTP 响应或凭据。"""

    def __init__(self, code: str, *, unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.unknown = unknown


def strict_json(value: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def constant(_value):
        raise ValueError("nonfinite number")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


class DeepSeekModel:
    """无 SDK、代理、重定向、重试、工具或备用模型。测试传输不由配置/浏览器提供。"""

    def __init__(self, key: str, *, _transport=None):
        self._key = key
        self._transport = _transport
        self.selected_ids: list[int] = []
        self.usage: dict[str, int] = {}
        self.provider_request_id: str | None = None

    def available(self) -> bool:
        return bool(self._key)

    def model_version(self) -> dict[str, str]:
        return {
            "capability": "llm",
            "model_name": MODEL,
            "revision": "api",
            "prompt_version": PROMPT_VERSION,
        }

    def caption(self, content: dict[str, Any]) -> None:
        return None

    def embed(self, content: dict[str, Any]) -> None:
        return None

    def classify(self, content: dict[str, Any]) -> ClassificationResult | None:
        self.selected_ids = []
        self.usage = {}
        self.provider_request_id = None
        if not self.available():
            return None
        options = {
            "verify": True,
            "trust_env": False,
            "follow_redirects": False,
            "timeout": httpx.Timeout(60, connect=5),
        }
        if self._transport is not None:
            options["transport"] = self._transport
        try:
            with (
                httpx.Client(**options) as client,
                client.stream(
                    "POST",
                    ENDPOINT,
                    content=content["request"],
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    code = (
                        "provider_auth"
                        if response.status_code in {401, 403}
                        else "provider_limit"
                        if response.status_code == 429
                        else "provider_failure"
                    )
                    raise ModelError(code)
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ModelError("invalid_output")
                raw = bytearray()
                for block in response.iter_bytes(chunk_size=8192):
                    if len(raw) + len(block) > RESPONSE_LIMIT:
                        raise ModelError("invalid_output")
                    raw.extend(block)
        except httpx.TimeoutException:
            raise ModelError("timeout", unknown=True) from None
        except httpx.RequestError:
            raise ModelError("network", unknown=True) from None
        try:
            if self._key.encode("utf-8") in raw:
                raise ValueError("credential echo")
            payload = strict_json(raw)
            if not isinstance(payload, dict):
                raise ValueError("envelope")
            request_id = payload.get("id")
            # JSON 转义不能绕过回显检查；下方正文理由在第二层解码后复查。
            if isinstance(request_id, str) and self._key in request_id:
                raise ValueError("credential echo")
            choices = payload["choices"]
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or choices[0]["finish_reason"] != "stop"
            ):
                raise ValueError("unfinished")
            message = choices[0]["message"]
            if (
                "tool_calls" in message
                or "function_call" in message
                or not isinstance(message["content"], str)
            ):
                raise ValueError("tools or nontext")
            parsed = strict_json(message["content"])
            if (
                not isinstance(parsed, dict)
                or set(parsed) != {"tags"}
                or not isinstance(parsed["tags"], list)
            ):
                raise ValueError("shape")
            catalog = {c["candidate_id"]: c for c in content["catalog"]}
            if not 1 <= len(parsed["tags"]) <= min(20, len(catalog)):
                raise ValueError("count")
            tags, selected, seen = [], [], set()
            for item in parsed["tags"]:
                if not isinstance(item, dict) or set(item) != {
                    "candidate_id",
                    "confidence",
                    "reason",
                }:
                    raise ValueError("fields")
                ident, score, reason = item["candidate_id"], item["confidence"], item["reason"]
                if type(ident) is not int or ident not in catalog or ident in seen:
                    raise ValueError("candidate")
                if (
                    type(score) not in {int, float}
                    or not math.isfinite(score)
                    or not 0 <= score <= 1
                ):
                    raise ValueError("confidence")
                if not isinstance(reason, str) or len(reason) > 160:
                    raise ValueError("reason")
                if self._key in reason:
                    raise ValueError("credential echo")
                seen.add(ident)
                candidate = catalog[ident]
                tags.append(
                    TagCandidate(
                        candidate["name"],
                        score,
                        {"reason": reason},
                        TagNamespace(candidate["namespace"]),
                    )
                )
                selected.append(candidate["tag_id"])
            self.selected_ids = selected
            usage = payload.get("usage")
            self.usage = (
                {
                    k: v
                    for k, v in usage.items()
                    if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                    and type(v) is int
                    and 0 <= v <= 2**63 - 1
                }
                if isinstance(usage, dict)
                else {}
            )
            self.provider_request_id = (
                request_id
                if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
                else None
            )
            return ClassificationResult(tags=tuple(tags), decided_by=MODEL)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
            raise ModelError("invalid_output") from None
