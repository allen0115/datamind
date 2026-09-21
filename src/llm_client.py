"""LLM 调用层:pm4py 与 LLM 之间唯一的出口。

设计遵循「LLM 只做语义与假设,pm4py 只做计算与判定」:

1. **结构化进出**:LLM 只收到压缩后的聚合指标,输出必须用 pydantic 模型校验。
   没有通过校验的输出一律视为调用失败,不会流到下游。
2. **同输入同输出**:temperature=0 + 磁盘缓存,缓存 key 含模板版本,
   改了 prompt 而不递增版本不会误命中旧缓存。
3. **离线可跑**:未启用 / 缺 API key / 未装 SDK 一律走 NullProvider,
   调用方捕获 LLMUnavailableError 后转规则化降级。全部测试无需网络。
4. **密钥不外泄**:API key 只读环境变量,不写 config.yaml、不进缓存、不进审计日志。

schema 一律用 pydantic:类型强制、缺失字段、嵌套结构由它负责,
本模块不再自己实现任何校验逻辑。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# 语义别名:LLM 输出未通过 schema 校验。用这个名字比 ValidationError 更能表达意图。
SchemaError = ValidationError


class LLMUnavailableError(RuntimeError):
    """LLM 不可用(未启用 / 缺 key / 未装 SDK / 网络失败)。调用方应降级。"""


# --------------------------------------------------------------------------
# Provider 抽象
# --------------------------------------------------------------------------

@dataclass
class Completion:
    """一次调用的结果。保持 dataclass:纯容器,obj 的类型由调用方决定。"""

    obj: Any
    tokens: int = 0
    model: str = ""
    cached: bool = False


class LLMProvider(Protocol):
    def complete(self, *, system: str, user: str,
                 schema: type[BaseModel]) -> Completion: ...


@dataclass
class OpenAIProvider:
    """真实调用。OpenAI 兼容协议,支持 base_url 指向自建/国内模型。"""

    model: str = "gpt-4o-mini"
    base_url: str | None = None
    api_key_env: str = "DATAMIND_LLM_API_KEY"
    timeout_s: float = 30.0
    temperature: float = 0.0
    max_retries: int = 1

    def complete(self, *, system: str, user: str,
                 schema: type[BaseModel]) -> Completion:
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover - 依赖未装时走降级
            raise LLMUnavailableError(
                "未安装 openai SDK,请执行 `uv sync --extra llm`;当前调用已降级") from e

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise LLMUnavailableError(f"环境变量 {self.api_key_env} 未设置,当前调用已降级")

        client = OpenAI(api_key=api_key, base_url=self.base_url or None,
                        timeout=self.timeout_s)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        hint = ""
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            if attempt:
                hint = (f"\n\n上一次输出不合法({last_err})。"
                        f"必须只输出一个 JSON 对象,字段与 schema 完全一致,"
                        f"不得包含解释文字。")
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=messages + ([{"role": "user", "content": hint}] if hint else []),
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                )
                raw = resp.choices[0].message.content or ""
                obj = schema.model_validate(json.loads(raw))
                tokens = int(getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0)
                return Completion(obj=obj, tokens=tokens, model=self.model)
            except (ValidationError, json.JSONDecodeError) as e:
                # 输出不合法:带上错误信息重试一次,用尽则判定本次调用失败
                last_err = e
                logger.warning("LLM 输出未通过 schema 校验(第 %d 次): %s", attempt + 1, e)
                continue
            except Exception as e:
                raise LLMUnavailableError(f"LLM 调用失败: {e}") from e

        raise LLMUnavailableError(f"LLM 输出连续 {self.max_retries + 1} 次未通过校验: {last_err}")


@dataclass
class ScriptedProvider:
    """离线 fake:预置响应队列,手写而非 mock 库,便于断言调用历史。"""

    responses: list[Any] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    model: str = "scripted"

    def complete(self, *, system: str, user: str,
                 schema: type[BaseModel]) -> Completion:
        self.calls.append({"system": system, "user": user, "schema": schema.__name__})
        if not self.responses:
            raise LLMUnavailableError("ScriptedProvider 响应队列已空")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, BaseModel):
            return Completion(obj=item, tokens=0, model=self.model)
        return Completion(obj=schema.model_validate(item), tokens=0, model=self.model)


class NullProvider:
    """未启用时的占位:一律抛错,由调用方转规则化降级。"""

    def complete(self, *, system: str, user: str,
                 schema: type[BaseModel]) -> Completion:
        raise LLMUnavailableError(
            "LLM 未启用(llm.enabled=false 或缺 API key),本次调用走规则化降级")


# --------------------------------------------------------------------------
# 客户端:缓存 + 审计
# --------------------------------------------------------------------------

@dataclass
class LLMClient:
    """包一层缓存与审计。缓存 key 含模板版本与 schema 名。"""

    provider: LLMProvider
    cache_dir: Path | None = None
    audit_path: Path | None = None
    template_version: str = "v1"
    enable_cache: bool = True

    @property
    def available(self) -> bool:
        return not isinstance(self.provider, NullProvider)

    def cache_key(self, *, system: str, user: str, schema: type[BaseModel]) -> str:
        payload = json.dumps([self.template_version, schema.__name__, system, user],
                             ensure_ascii=False, sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()[:32]

    def complete(self, *, system: str, user: str,
                 schema: type[BaseModel]) -> Completion:
        key = self.cache_key(system=system, user=user, schema=schema)
        started = time.monotonic()

        if self.enable_cache and self.cache_dir is not None:
            cached = self.cache_dir / f"{key}.json"
            if cached.exists():
                obj = schema.model_validate(json.loads(cached.read_text(encoding="utf-8")))
                self._audit(key, schema, 0, 0, True, self._model_name())
                return Completion(obj=obj, tokens=0, model=self._model_name(), cached=True)

        try:
            comp = self.provider.complete(system=system, user=user, schema=schema)
        except Exception:
            self._audit(key, schema, int((time.monotonic() - started) * 1000), 0,
                        False, "", ok=False)
            raise

        if self.enable_cache and self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps(_to_jsonable(comp.obj), ensure_ascii=False), encoding="utf-8")

        self._audit(key, schema, int((time.monotonic() - started) * 1000),
                    comp.tokens, False, comp.model)
        return comp

    def _model_name(self) -> str:
        return getattr(self.provider, "model", "")

    def _audit(self, key: str, schema: type[BaseModel], elapsed_ms: int,
               tokens: int, cached: bool, model: str, ok: bool = True) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "template_version": self.template_version,
            "schema": schema.__name__,
            "cache_key": key,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "tokens": tokens,
            "cached": cached,
            "ok": ok,
        }
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------
# 配置装配
# --------------------------------------------------------------------------

_DEFAULTS = {
    "enabled": False,
    "model": "gpt-4o-mini",
    "base_url": "",
    "api_key_env": "DATAMIND_LLM_API_KEY",
    "timeout_s": 30,
    "temperature": 0,
    "max_retries": 1,
    "cache_dir": ".cache/llm",
    "audit_path": "outputs/llm_audit.jsonl",
    "template_version": "v1",
    "max_variants": 8,
}


def llm_settings(cfg: dict | None) -> dict:
    """合并默认值的 LLM 配置段。"""
    out = dict(_DEFAULTS)
    out.update((cfg or {}).get("llm", {}) or {})
    return out


def build_llm_client(cfg: dict | None, root: str | Path = ".") -> LLMClient:
    """按 config.yaml 装配客户端。未启用一律返回 NullProvider 客户端。"""
    s = llm_settings(cfg)
    root = Path(root)

    if not s.get("enabled"):
        provider: LLMProvider = NullProvider()
        cache_dir = None
    else:
        provider = OpenAIProvider(
            model=str(s["model"]),
            base_url=str(s.get("base_url") or "") or None,
            api_key_env=str(s.get("api_key_env") or "DATAMIND_LLM_API_KEY"),
            timeout_s=float(s.get("timeout_s", 30)),
            temperature=float(s.get("temperature", 0)),
            max_retries=int(s.get("max_retries", 1)),
        )
        cache_dir = root / str(s.get("cache_dir") or ".cache/llm")

    return LLMClient(
        provider=provider,
        cache_dir=cache_dir,
        audit_path=root / str(s.get("audit_path") or "outputs/llm_audit.jsonl"),
        template_version=str(s.get("template_version") or "v1"),
    )


def scripted_client(responses: list[Any], **kwargs) -> tuple[LLMClient, ScriptedProvider]:
    """测试用:返回 (client, provider),便于断言调用历史。"""
    provider = ScriptedProvider(responses=responses)
    return LLMClient(provider=provider, **kwargs), provider
