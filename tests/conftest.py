"""pytest 全局夹具。

关键一条:**普通测试必须与开发机上的 .env 隔离**。
否则同一个测试在本机(有 key、enabled=true)和 CI(无 .env)会得到不同结果,
这本身就是"配置泄漏进测试"的典型事故。

例外:标记 `e2e` 的用例就是要打真实模型,不隔离(它会自己读 .env)。
"""
from __future__ import annotations

import os

import pytest

_ENV_PREFIX = "DATAMIND_LLM_"


@pytest.fixture(autouse=True)
def _isolate_llm_env(request, monkeypatch):
    """清空 LLM 相关环境变量并屏蔽 .env 加载;e2e 用例除外。"""
    if request.node.get_closest_marker("e2e"):
        return

    import src.llm_client as llm_client

    monkeypatch.setattr(llm_client, "_DOTENV_LOADED", True)
    for k in list(os.environ):
        if k.startswith(_ENV_PREFIX):
            monkeypatch.delenv(k, raising=False)
