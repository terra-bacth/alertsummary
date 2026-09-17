"""LiteLLM gateway: one call path for vLLM, Ollama, Gemini, OpenAI and Anthropic.

Switching provider is a config edit (``config/llm.yaml`` or the ``TSAGENT_LLM_PROFILE`` env var);
no agent code knows which backend it is talking to. Profiles are resolved per agent so the
planner can run on a different model than reflection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """Raised when every profile in the chain failed, or a profile is unusable."""


_JSON_INSTRUCTION = "Respond with a single valid JSON object only — no prose, no code fence."

# Substrings seen when a server rejects the response_format field itself rather than the request.
# vLLM surfaces guided-decoding backend failures (outlines / xgrammar) this way.
_GUIDED_DECODING_ERRORS = (
    "guided",
    "response_format",
    "json_object",
    "structured output",
    "outlines",
    "xgrammar",
    "lm-format-enforcer",
)


def _nudge_json(messages: list[dict]) -> list[dict]:
    """Add a JSON-only instruction without mutating the caller's list.

    The instruction is merged into the leading system message rather than appended as a second
    one: Llama and Qwen chat templates expect a single system turn at the front, and a trailing
    system message is either dropped or renders oddly depending on the template.
    """
    copied = [dict(m) for m in messages]
    for message in copied:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content', '')}\n{_JSON_INSTRUCTION}".strip()
            return copied
    return [{"role": "system", "content": _JSON_INSTRUCTION}, *copied]


def _is_guided_decoding_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _GUIDED_DECODING_ERRORS)


def _resolves(model: str) -> bool:
    """Whether LiteLLM can route this model name on its own.

    Asking LiteLLM directly beats maintaining our own prefix list: bare names for the big public
    providers (``claude-sonnet-5``, ``gpt-4o``) resolve without a prefix, while a served model
    name like ``Qwen/Qwen2.5-VL-7B-Instruct`` does not — ``Qwen`` is a HuggingFace org, not a
    provider.
    """
    try:
        from litellm import get_llm_provider
    except ImportError:  # litellm is an optional extra
        return True
    try:
        get_llm_provider(model=model)
    except Exception:  # noqa: BLE001 - any resolution failure means "not routable"
        return False
    return True


@dataclass
class LLMResponse:
    content: str
    model: str
    profile: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)


class ProfileConfig(BaseModel):
    model: str
    api_base: str | None = None
    api_key_env: str = ""
    timeout_seconds: int = 300
    max_tokens: int = 1024
    temperature: float = 0.1
    max_attempts: int = 3
    retry_backoff_seconds: float = 2.0
    # How to ask for JSON: "response_format" uses the OpenAI field (vLLM turns this into guided
    # decoding, which some builds/backends reject outright); "prompt" just instructs the model.
    structured_output: str = "response_format"


class LLMGateway:
    """Async LiteLLM wrapper with per-agent profiles, retries and a fallback chain."""

    def __init__(self, config_path: Path | str = Path("config/llm.yaml")):
        self.config_path = Path(config_path)
        raw = self._load_raw()
        self.defaults: dict[str, Any] = raw.get("defaults") or {}
        self.default_profile: str = os.environ.get(
            "TSAGENT_LLM_PROFILE", raw.get("default_profile", "llama-lab")
        )
        self.per_agent_overrides: dict[str, str] = raw.get("per_agent_overrides") or {}
        self.fallback_chain: list[str] = raw.get("fallback_chain") or []
        self.profiles: dict[str, ProfileConfig] = {
            name: ProfileConfig.model_validate({**self.defaults, **(cfg or {})})
            for name, cfg in (raw.get("profiles") or {}).items()
        }
        if not self.profiles:
            raise LLMError(f"no LLM profiles defined in {self.config_path}")
        self._normalize_models()

    def _normalize_models(self) -> None:
        """Ensure every model carries a LiteLLM provider prefix, and say so in the log.

        LiteLLM routes on a ``<provider>/<model>`` prefix. A served model name like
        ``Qwen/Qwen2.5-VL-7B-Instruct`` looks prefixed but ``Qwen`` is a HuggingFace org, not a
        provider, so the call fails instantly with "LLM Provider NOT provided" — once per agent,
        per incident, indefinitely. When the profile has an api_base it is an OpenAI-compatible
        endpoint and the prefix is inferable; when it does not, the config is simply wrong and
        should fail at boot rather than quietly degrade every investigation.
        """
        for name, profile in self.profiles.items():
            if _resolves(profile.model):
                continue
            if profile.api_base:
                fixed = f"hosted_vllm/{profile.model}"
                logger.warning(
                    "profile '%s': model %r has no LiteLLM provider prefix; assuming an "
                    "OpenAI-compatible endpoint and using %r. Set this explicitly in llm.yaml.",
                    name, profile.model, fixed,
                )
                profile.model = fixed
            else:
                raise LLMError(
                    f"profile '{name}': model {profile.model!r} has no LiteLLM provider prefix "
                    f"and no api_base to infer one from. Use e.g. 'hosted_vllm/{profile.model}' "
                    f"for a vLLM endpoint, or 'openai/...', 'ollama/...', 'anthropic/...'."
                )
        logger.info(
            "LLM profiles: %s",
            ", ".join(f"{n}->{p.model}" for n, p in sorted(self.profiles.items())),
        )

    def _load_raw(self) -> dict:
        if not self.config_path.exists():
            logger.warning("%s missing, falling back to a bare ollama profile", self.config_path)
            return {
                "default_profile": "ollama",
                "profiles": {"ollama": {"model": "ollama/llama3.2:3b"}},
            }
        with open(self.config_path) as f:
            return yaml.safe_load(f) or {}

    # -- profile resolution -------------------------------------------------

    def profile_name_for(self, agent_name: str) -> str:
        return self.per_agent_overrides.get(agent_name, self.default_profile)

    def _chain_for(self, agent_name: str) -> list[str]:
        primary = self.profile_name_for(agent_name)
        chain = [primary] + [p for p in self.fallback_chain if p != primary]
        known = [p for p in chain if p in self.profiles]
        if not known:
            raise LLMError(f"profile '{primary}' for agent '{agent_name}' is not defined")
        return known

    # -- completion ---------------------------------------------------------

    async def complete(
        self,
        agent_name: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Run a completion for ``agent_name``, walking the fallback chain on failure."""
        last_error: Exception | None = None
        for profile_name in self._chain_for(agent_name):
            profile = self.profiles[profile_name]
            for attempt in range(1, profile.max_attempts + 1):
                try:
                    return await self._call(
                        agent_name, profile_name, profile, messages, tools, max_tokens, json_mode
                    )
                except Exception as exc:  # noqa: BLE001 - provider errors are opaque by nature
                    last_error = exc
                    # A server that rejects response_format will reject it every time, so flip
                    # this profile to prompt-based JSON for the rest of the process rather than
                    # burning the whole retry budget — and every future call — on the same error.
                    if (
                        json_mode
                        and profile.structured_output == "response_format"
                        and _is_guided_decoding_error(exc)
                    ):
                        profile.structured_output = "prompt"
                        logger.warning(
                            "profile '%s' rejected response_format (%s); falling back to "
                            "prompt-based JSON for this profile. Set structured_output: prompt "
                            "in llm.yaml to make this permanent.",
                            profile_name, str(exc)[:160],
                        )
                        # Retried here rather than by looping, so diagnosing the server's own
                        # config does not eat an attempt that the model may still need.
                        try:
                            return await self._call(
                                agent_name, profile_name, profile, messages, tools,
                                max_tokens, json_mode,
                            )
                        except Exception as retry_exc:  # noqa: BLE001
                            last_error = retry_exc
                            logger.warning(
                                "llm call failed after dropping response_format: "
                                "agent=%s profile=%s: %s",
                                agent_name, profile_name, retry_exc,
                            )
                    else:
                        logger.warning(
                            "llm call failed: agent=%s profile=%s attempt=%d/%d: %s",
                            agent_name, profile_name, attempt, profile.max_attempts, exc,
                        )
                    if attempt < profile.max_attempts and profile.retry_backoff_seconds:
                        await asyncio.sleep(
                            min(profile.retry_backoff_seconds * 2 ** (attempt - 1), 10)
                        )
        raise LLMError(f"all LLM profiles failed for agent '{agent_name}': {last_error}")

    async def _call(
        self,
        agent_name: str,
        profile_name: str,
        profile: ProfileConfig,
        messages: list[dict],
        tools: list[dict] | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> LLMResponse:
        from litellm import acompletion  # imported lazily: heavy, and unused in unit tests

        kwargs: dict[str, Any] = {
            "model": profile.model,
            "messages": messages,
            "timeout": profile.timeout_seconds,
            "temperature": profile.temperature,
            "max_tokens": max_tokens or profile.max_tokens,
        }
        if profile.api_base:
            kwargs["api_base"] = profile.api_base
        if profile.api_key_env:
            key = os.environ.get(profile.api_key_env)
            if key:
                kwargs["api_key"] = key
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if json_mode:
            if profile.structured_output == "response_format":
                kwargs["response_format"] = {"type": "json_object"}
            else:
                kwargs["messages"] = _nudge_json(messages)

        started = time.perf_counter()
        response = await acompletion(**kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)

        choice = response.choices[0].message
        raw_tool_calls = getattr(choice, "tool_calls", None) or []
        tool_calls = [
            {
                "id": getattr(tc, "id", ""),
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            }
            for tc in raw_tool_calls
        ]
        usage = getattr(response, "usage", None)
        result = LLMResponse(
            content=choice.content or "",
            model=profile.model,
            profile=profile_name,
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            tool_calls=tool_calls,
        )
        logger.info(
            "llm ok: agent=%s profile=%s model=%s latency=%dms tokens=%d/%d tools=%d",
            agent_name, profile_name, profile.model, latency_ms,
            result.prompt_tokens, result.completion_tokens, len(tool_calls),
        )
        return result

    # -- structured output --------------------------------------------------

    async def complete_json(
        self,
        agent_name: str,
        messages: list[dict],
        schema: type[T],
        max_tokens: int | None = None,
    ) -> T:
        """Complete and validate against ``schema``, re-prompting once on a parse failure."""
        response = await self.complete(agent_name, messages, max_tokens=max_tokens, json_mode=True)
        try:
            return schema.model_validate(extract_json(response.content))
        except (ValueError, ValidationError) as exc:
            logger.warning("structured output invalid for agent=%s: %s", agent_name, exc)
            repair = messages + [
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": (
                        f"That was not valid for the required schema ({exc}). "
                        "Reply with the corrected JSON object only — no prose, no code fence."
                    ),
                },
            ]
            retry = await self.complete(agent_name, repair, max_tokens=max_tokens, json_mode=True)
            try:
                return schema.model_validate(extract_json(retry.content))
            except (ValueError, ValidationError) as exc2:
                raise LLMError(f"agent '{agent_name}' produced unusable JSON: {exc2}") from exc2


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model response, tolerating fences and surrounding prose."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = _JSON_FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"no JSON object found in response: {text[:200]!r}")
