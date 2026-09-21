"""OpenAIProvider 真实代码路径测试(不联网)。

用一个假的 openai 模块替身注入 sys.modules,从而真正走一遍
OpenAIProvider.complete():prompt 组装、response_format、temperature、
schema 校验、失败重试、异常降级。网络与鉴权由替身承担。

这样"真实调用"这条链路里除了最后一跳(HTTP),其余都被验证过。
"""
from __future__ import annotations

import json
import sys
import types

import pytest
from pydantic import BaseModel

from src.llm_client import LLMUnavailableError, OpenAIProvider


class Reply(BaseModel):
    title: str
    score: float


class _Msg:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.message = _Msg(content)


class _Usage:
    total_tokens = 137


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, recorder, contents, fail_with=None):
        self.recorder = recorder
        self.contents = list(contents)
        self.fail_with = fail_with

    def create(self, **kwargs):
        self.recorder.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with
        return _Resp(self.contents.pop(0))


class _Chat:
    def __init__(self, completions): self.completions = completions


class _Client:
    def __init__(self, recorder, contents, fail_with=None, ctor=None):
        if ctor is not None:
            ctor.append(True)
        self.chat = _Chat(_Completions(recorder, contents, fail_with))


@pytest.fixture
def fake_openai(monkeypatch):
    """注入假 openai 模块,返回 (调用记录, 响应内容队列, 构造记录, 失败注入器)。"""
    calls: list[dict] = []
    contents: list[str] = []
    ctor_calls: list[dict] = []
    state = {"fail_with": None}

    mod = types.ModuleType("openai")

    def OpenAI(**kwargs):  # noqa: N802 - 刻意与 SDK 同名
        ctor_calls.append(kwargs)
        return _Client(calls, contents, state["fail_with"])

    mod.OpenAI = OpenAI
    monkeypatch.setitem(sys.modules, "openai", mod)
    return calls, contents, ctor_calls, state


def _provider(tmp_path, monkeypatch, model="deepseek-chat",
              base_url="https://api.deepseek.com") -> OpenAIProvider:
    monkeypatch.setenv("DATAMIND_LLM_API_KEY", "sk-test")
    return OpenAIProvider(model=model, base_url=base_url, timeout_s=5.0)


def test_success_path_builds_openai_compatible_request(fake_openai, tmp_path, monkeypatch):
    calls, contents, ctor_calls, _ = fake_openai
    contents.append(json.dumps({"title": "a", "score": 1.5}))

    p = _provider(tmp_path, monkeypatch)
    comp = p.complete(system="SYS", user="USR", schema=Reply)

    assert comp.obj.title == "a" and comp.obj.score == 1.5
    assert comp.tokens == 137 and comp.model == "deepseek-chat"
    # OpenAI 兼容协议:base_url 透传、temperature=0、JSON 输出模式
    assert ctor_calls[0]["base_url"] == "https://api.deepseek.com"
    assert calls[0]["temperature"] == 0
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["messages"][0] == {"role": "system", "content": "SYS"}
    assert calls[0]["messages"][1] == {"role": "user", "content": "USR"}


def test_schema_error_retries_once_with_correction_hint(fake_openai, tmp_path, monkeypatch):
    calls, contents, _, _ = fake_openai
    contents.append(json.dumps({"title": "a"}))                       # 缺 score
    contents.append(json.dumps({"title": "b", "score": 2.0}))         # 修正后

    p = _provider(tmp_path, monkeypatch)
    comp = p.complete(system="SYS", user="USR", schema=Reply)

    assert comp.obj.score == 2.0
    assert len(calls) == 2, "schema 不通过应重试一次"
    assert "不合法" in calls[1]["messages"][-1]["content"], "重试时要带上修正意见"


def test_exhausted_retries_raise_unavailable(fake_openai, tmp_path, monkeypatch):
    _, contents, _, _ = fake_openai
    contents.extend([json.dumps({"title": "a"}), json.dumps({"title": "a"})])

    p = _provider(tmp_path, monkeypatch)
    with pytest.raises(LLMUnavailableError):
        p.complete(system="SYS", user="USR", schema=Reply)


def test_transport_error_is_wrapped(fake_openai, tmp_path, monkeypatch):
    _, _, _, state = fake_openai
    state["fail_with"] = RuntimeError("connection reset")

    p = _provider(tmp_path, monkeypatch)
    with pytest.raises(LLMUnavailableError, match="调用失败"):
        p.complete(system="SYS", user="USR", schema=Reply)


def test_missing_api_key_degrades(monkeypatch):
    monkeypatch.delenv("DATAMIND_LLM_API_KEY", raising=False)
    p = OpenAIProvider()
    with pytest.raises(LLMUnavailableError, match="未设置"):
        p.complete(system="s", user="u", schema=Reply)


def test_env_vars_configure_deepseek(monkeypatch, tmp_path):
    """.env 里的变量应能直接配出 DeepSeek 客户端。"""
    import yaml

    from src.llm_client import build_llm_client

    monkeypatch.setenv("DATAMIND_LLM_ENABLED", "true")
    monkeypatch.setenv("DATAMIND_LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("DATAMIND_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DATAMIND_LLM_API_KEY", "sk-test")

    cfg = yaml.safe_load((tmp_path / "cfg.yaml").write_text("llm:\n  enabled: false\n")
                         and (tmp_path / "cfg.yaml").read_text())
    client = build_llm_client(cfg, root=tmp_path)
    assert client.available is True
    assert client.provider.model == "deepseek-chat"
    assert client.provider.base_url == "https://api.deepseek.com"
