"""LLM 调用层测试(全程离线,不真调模型)。

schema 校验由 pydantic 负责,这里只验证「我们的用法」是否正确:
类型强制、必填校验、ScriptedProvider 队列、NullProvider 降级、
缓存命中与模板版本隔离、审计落盘。
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field, ValidationError

from src.llm_client import (
    Completion,
    LLMClient,
    LLMUnavailableError,
    NullProvider,
    OpenAIProvider,
    ScriptedProvider,
    build_llm_client,
    llm_settings,
)


class Leaf(BaseModel):
    name: str
    score: float


class Node(BaseModel):
    title: str
    count: int
    flag: bool
    leaves: list[Leaf] = Field(default_factory=list)
    note: str | None = None


# --------------------------------------------------------------------------
# pydantic schema 契约
# --------------------------------------------------------------------------

def test_model_validate_coerces_types():
    obj = Node.model_validate({"title": "x", "count": "12", "flag": True,
                               "leaves": [{"name": "a", "score": "1.5"}]})
    assert obj.count == 12 and isinstance(obj.count, int)
    assert obj.leaves[0].score == 1.5
    assert obj.note is None


def test_missing_required_field_raises():
    with pytest.raises(ValidationError):
        Node.model_validate({"title": "x"})


def test_wrong_type_raises():
    with pytest.raises(ValidationError):
        Node.model_validate({"title": "x", "count": "abc", "flag": True})


def test_non_object_input_raises():
    with pytest.raises(ValidationError):
        Node.model_validate([1, 2, 3])


def test_bool_is_coerced_to_int_by_pydantic():
    """pydantic 宽松模式下 bool 会被当成 int(True→1)。

    这是 pydantic 的行为而非我们的逻辑;真要拦住布尔值需要 strict 模式或
    自定义校验器。这里把行为固化下来,避免以后误以为已经挡住了。
    """
    assert Node.model_validate({"title": "x", "count": True, "flag": True}).count == 1


def test_model_json_schema_contains_required():
    s = Node.model_json_schema()
    assert s["type"] == "object"
    assert set(s["required"]) == {"title", "count", "flag"}
    assert s["properties"]["leaves"]["type"] == "array"


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------

def test_scripted_provider_returns_in_order_and_records_calls():
    p = ScriptedProvider(responses=[{"title": "a", "count": 1, "flag": False},
                                    {"title": "b", "count": 2, "flag": True}])
    first = p.complete(system="s", user="u", schema=Node)
    second = p.complete(system="s2", user="u2", schema=Node)
    assert first.obj.title == "a" and second.obj.title == "b"
    assert len(p.calls) == 2 and p.calls[0]["schema"] == "Node"


def test_scripted_provider_accepts_model_instance():
    p = ScriptedProvider(responses=[Node(title="a", count=1, flag=False)])
    assert p.complete(system="s", user="u", schema=Node).obj.title == "a"


def test_scripted_provider_empty_queue_raises():
    with pytest.raises(LLMUnavailableError):
        ScriptedProvider(responses=[]).complete(system="s", user="u", schema=Node)


def test_scripted_provider_invalid_payload_raises_validation_error():
    p = ScriptedProvider(responses=[{"title": "a"}])
    with pytest.raises(ValidationError):
        p.complete(system="s", user="u", schema=Node)


def test_null_provider_always_unavailable():
    with pytest.raises(LLMUnavailableError):
        NullProvider().complete(system="s", user="u", schema=Node)


def test_openai_provider_unavailable_without_sdk():
    """本环境未装 openai:调用必须降级为 LLMUnavailableError,而不是 ImportError。"""
    p = OpenAIProvider(api_key_env="DATAMIND_TEST_KEY_MISSING")
    with pytest.raises(LLMUnavailableError):
        p.complete(system="s", user="u", schema=Node)


# --------------------------------------------------------------------------
# 客户端:缓存 / 审计
# --------------------------------------------------------------------------

def test_cache_hit_skips_provider(tmp_path):
    client, provider = _client(tmp_path, responses=[{"title": "a", "count": 1, "flag": False}])
    first = client.complete(system="s", user="u", schema=Node)
    second = client.complete(system="s", user="u", schema=Node)
    assert first.obj.title == second.obj.title == "a"
    assert len(provider.calls) == 1, "第二次应命中缓存,不再调用 provider"
    assert second.cached is True
    assert (tmp_path / f"{client.cache_key(system='s', user='u', schema=Node)}.json").exists()


def test_cache_key_differs_by_template_version(tmp_path):
    client, provider = _client(tmp_path,
                               responses=[{"title": "a", "count": 1, "flag": False},
                                         {"title": "b", "count": 2, "flag": True}])
    client.complete(system="s", user="u", schema=Node)
    client.template_version = "v2"
    client.complete(system="s", user="u", schema=Node)
    assert len(provider.calls) == 2, "模板版本变化必须绕过旧缓存"


def test_cache_key_differs_by_user_payload(tmp_path):
    client, provider = _client(tmp_path,
                               responses=[{"title": "a", "count": 1, "flag": False},
                                         {"title": "b", "count": 2, "flag": True}])
    client.complete(system="s", user="u1", schema=Node)
    client.complete(system="s", user="u2", schema=Node)
    assert len(provider.calls) == 2


def test_cached_model_is_revalidated(tmp_path):
    client, _ = _client(tmp_path, responses=[{"title": "a", "count": 1, "flag": False}])
    client.complete(system="s", user="u", schema=Node)
    obj = client.complete(system="s", user="u", schema=Node).obj
    assert isinstance(obj, Node) and obj.count == 1


def test_audit_written_without_prompt_text(tmp_path):
    client, _ = _client(tmp_path, responses=[{"title": "a", "count": 1, "flag": False}])
    client.complete(system="机密 system", user="机密 user", schema=Node)
    lines = [json.loads(x) for x in
             (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["schema"] == "Node" and rec["ok"] is True
    assert "机密" not in json.dumps(rec, ensure_ascii=False), "审计不得记录 prompt 原文"


def test_audit_records_failure(tmp_path):
    client, _ = _client(tmp_path, responses=[LLMUnavailableError("boom")])
    with pytest.raises(LLMUnavailableError):
        client.complete(system="s", user="u", schema=Node)
    rec = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["ok"] is False


# --------------------------------------------------------------------------
# 配置装配
# --------------------------------------------------------------------------

def test_llm_settings_defaults_to_disabled():
    s = llm_settings({})
    assert s["enabled"] is False
    assert s["max_variants"] == 8


def test_build_client_disabled_returns_null_provider():
    client = build_llm_client({"llm": {"enabled": False}}, root=".")
    assert isinstance(client.provider, NullProvider)
    assert client.available is False


def test_build_client_enabled_uses_openai_provider(tmp_path):
    client = build_llm_client({"llm": {"enabled": True, "model": "m1"}}, root=tmp_path)
    assert isinstance(client.provider, OpenAIProvider)
    assert client.provider.model == "m1"
    assert client.available is True
    assert client.cache_dir == tmp_path / ".cache/llm"


def test_completion_defaults():
    c = Completion(obj=None)
    assert c.tokens == 0 and c.cached is False


def _client(tmp_path, responses) -> tuple[LLMClient, ScriptedProvider]:
    provider = ScriptedProvider(responses=responses)
    return LLMClient(provider=provider, cache_dir=tmp_path,
                     audit_path=tmp_path / "audit.jsonl"), provider
