"""固定提供商适配器：仅传输已准备字节，严格解析且不重试。"""

from __future__ import annotations

import json

import httpx
import pytest


def envelope(
    output='{"tags":[{"candidate_id":1,"confidence":0.8,"reason":"匹配正文"}]}', **message
):
    return {
        "id": "safe-id",
        "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        "choices": [{"finish_reason": "stop", "message": {"content": output, **message}}],
    }


def invoke(response, *, status=200):
    from pc.adapters.deepseek_model import DeepSeekModel

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=response)

    provider = DeepSeekModel("TEST-ONLY-SECRET", _transport=httpx.MockTransport(handle))
    result = provider.classify(
        {
            "request": b'{"model":"deepseek-flash"}',
            "catalog": [{"candidate_id": 1, "tag_id": 7, "name": "标签", "namespace": "general"}],
        }
    )
    return provider, result, calls


def test_fixed_endpoint_and_existing_candidate_mapping():
    provider, result, calls = invoke(envelope())
    assert len(calls) == 1
    assert str(calls[0].url) == "https://api.deepseek.com/chat/completions"
    assert calls[0].content == b'{"model":"deepseek-flash"}'
    assert result.tags[0].tag == "标签"
    assert result.tags[0].evidence == {"reason": "匹配正文"}
    assert provider.selected_ids == [7]
    assert provider.model_version()["prompt_version"] == "tagging-v1"
    assert provider.caption({}) is None and provider.embed({}) is None


@pytest.mark.parametrize(
    "output",
    [
        '{"tags":[]}',
        '{"tags":[{"candidate_id":2,"confidence":0.8,"reason":""}]}',
        '{"tags":[{"candidate_id":true,"confidence":0.8,"reason":""}]}',
        '{"tags":[{"candidate_id":1,"confidence":true,"reason":""}]}',
        '{"tags":[{"candidate_id":1,"confidence":NaN,"reason":""}]}',
        '{"tags":[{"candidate_id":1,"confidence":1.1,"reason":""}]}',
        '{"tags":[{"candidate_id":1,"confidence":0.8,"reason":"","new_tag":"x"}]}',
        '{"tags":[{"candidate_id":1,"confidence":0.8,"reason":"","candidate_id":1}]}',
        '{"tags":[{"candidate_id":1,"confidence":0.8,"reason":""},{"candidate_id":1,"confidence":0.7,"reason":""}]}',
        json.dumps({"tags": [{"candidate_id": 1, "confidence": 0.8, "reason": "x" * 161}]}),
        '```json\n{"tags":[]}\n```',
    ],
)
def test_rejects_entire_invalid_response(output):
    from pc.adapters.deepseek_model import ModelError

    with pytest.raises(ModelError, match="invalid_output"):
        invoke(envelope(output))


def test_rejects_tools_length_redirect_and_never_retries():
    from pc.adapters.deepseek_model import ModelError

    for response, status in [(envelope(tool_calls=[]), 200), (envelope(), 302), (envelope(), 401)]:
        with pytest.raises(ModelError) as caught:
            invoke(response, status=status)
        assert "TEST-ONLY-SECRET" not in str(caught.value)
    response = envelope()
    response["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ModelError):
        invoke(response)


def test_transport_policy_and_bounded_response(monkeypatch):
    from pc.adapters.deepseek_model import DeepSeekModel, ModelError

    original = httpx.Client
    observed = {}
    calls = []

    def factory(**kwargs):
        observed.update(kwargs)

        def handle(request):
            calls.append(request)
            return httpx.Response(200, content=b"x" * (256 * 1024 + 1))

        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "Client", factory)
    provider = DeepSeekModel("test")
    with pytest.raises(ModelError, match="invalid_output"):
        provider.classify({"request": b"{}", "catalog": []})
    assert len(calls) == 1
    assert observed["trust_env"] is False and observed["follow_redirects"] is False
    assert observed["verify"] is True
    assert observed["timeout"].connect == 5 and observed["timeout"].read == 60


def test_failed_call_clears_previous_response_metadata():
    from pc.adapters.deepseek_model import DeepSeekModel, ModelError

    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200 if len(calls) == 1 else 401, json=envelope())

    provider = DeepSeekModel("TEST-ONLY-SECRET", _transport=httpx.MockTransport(handle))
    content = {
        "request": b"{}",
        "catalog": [{"candidate_id": 1, "tag_id": 7, "name": "标签", "namespace": "general"}],
    }
    provider.classify(content)
    assert provider.usage == {"prompt_tokens": 10, "completion_tokens": 8}
    assert provider.provider_request_id == "safe-id"
    with pytest.raises(ModelError, match="provider_auth"):
        provider.classify(content)
    assert len(calls) == 2
    assert provider.selected_ids == []
    assert provider.usage == {}
    assert provider.provider_request_id is None


def test_credential_echo_in_otherwise_valid_output_is_rejected():
    from pc.adapters.deepseek_model import ModelError

    output = json.dumps(
        {"tags": [{"candidate_id": 1, "confidence": 0.8, "reason": "TEST-ONLY-SECRET"}]}
    )
    with pytest.raises(ModelError, match=r"^invalid_output$") as caught:
        invoke(envelope(output))
    assert "TEST-ONLY-SECRET" not in str(caught.value)


@pytest.mark.parametrize('destination', ['reason', 'id'])
def test_json_escaped_credential_echo_is_rejected(destination):
    from pc.adapters.deepseek_model import DeepSeekModel, ModelError

    secret = 'TEST-ONLY-SECRET'
    escaped = ''.join(f'\\u{ord(char):04x}' for char in secret)
    payload = envelope()
    if destination == 'reason':
        payload['choices'][0]['message']['content'] = (
            '{"tags":[{"candidate_id":1,"confidence":0.8,"reason":"' + escaped + '"}]}'
        )
        raw = json.dumps(payload).encode()
    else:
        payload['id'] = secret
        raw = json.dumps(payload).replace(secret, escaped).encode()
    assert secret.encode() not in raw
    provider = DeepSeekModel(secret, _transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=raw)
    ))
    with pytest.raises(ModelError, match=r'^invalid_output$') as caught:
        provider.classify({
            'request': b'{}',
            'catalog': [{'candidate_id': 1, 'tag_id': 7, 'name': '标签', 'namespace': 'general'}],
        })
    assert secret not in str(caught.value)
    assert provider.selected_ids == [] and provider.provider_request_id is None


def test_timeout_is_unknown_and_sanitized():
    from pc.adapters.deepseek_model import DeepSeekModel, ModelError

    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret provider text", request=request)

    provider = DeepSeekModel("secret", _transport=httpx.MockTransport(fail))
    with pytest.raises(ModelError) as caught:
        provider.classify({"request": b"{}", "catalog": []})
    assert caught.value.unknown is True
    assert str(caught.value) == "timeout"
    assert len(calls) == 1
