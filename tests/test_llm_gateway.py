"""LLM gateway: profile resolution, retries, fallback and structured-output repair."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml
from pydantic import BaseModel

from tsagent.llm import LLMError, LLMGateway
from tsagent.llm.gateway import extract_json

CONFIG = {
    "default_profile": "llama-lab",
    "defaults": {
        "timeout_seconds": 30,
        "max_tokens": 256,
        "temperature": 0.1,
        "max_attempts": 2,
        "retry_backoff_seconds": 0,  # the backoff is real in production; waiting for it here is not
    },
    "profiles": {
        "llama-lab": {"model": "hosted_vllm/llama", "api_base": "http://vllm:8000/v1"},
        "qwen-lab": {"model": "hosted_vllm/qwen", "api_base": "http://qwen:8000/v1"},
        "anthropic": {"model": "claude-sonnet-5", "api_key_env": "ANTHROPIC_API_KEY"},
    },
    "per_agent_overrides": {"planner": "qwen-lab"},
    "fallback_chain": ["anthropic"],
}


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump(CONFIG))
    return path


def fake_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


@pytest.fixture
def patch_litellm(monkeypatch):
    """Install a scripted acompletion and record the kwargs each call received."""

    def install(handler):
        import litellm

        calls: list[dict] = []

        async def acompletion(**kwargs):
            calls.append(kwargs)
            return handler(kwargs, len(calls))

        monkeypatch.setattr(litellm, "acompletion", acompletion)
        return calls

    return install


# -- profile resolution -----------------------------------------------------


def test_per_agent_override_selects_profile(config_file):
    gateway = LLMGateway(config_file)
    assert gateway.profile_name_for("planner") == "qwen-lab"
    assert gateway.profile_name_for("reflection") == "llama-lab"


def test_env_var_overrides_default_profile(config_file, monkeypatch):
    monkeypatch.setenv("TSAGENT_LLM_PROFILE", "anthropic")
    gateway = LLMGateway(config_file)
    assert gateway.profile_name_for("reflection") == "anthropic"
    # An explicit per-agent override still wins over the env default.
    assert gateway.profile_name_for("planner") == "qwen-lab"


def test_profile_inherits_defaults(config_file):
    gateway = LLMGateway(config_file)
    assert gateway.profiles["llama-lab"].timeout_seconds == 30
    assert gateway.profiles["llama-lab"].max_tokens == 256


# -- calling ----------------------------------------------------------------


async def test_complete_passes_profile_settings(config_file, patch_litellm):
    calls = patch_litellm(lambda kwargs, n: fake_response("hello"))
    gateway = LLMGateway(config_file)

    response = await gateway.complete("planner", [{"role": "user", "content": "hi"}])

    assert response.content == "hello"
    assert response.profile == "qwen-lab"
    assert calls[0]["model"] == "hosted_vllm/qwen"
    assert calls[0]["api_base"] == "http://qwen:8000/v1"
    assert calls[0]["timeout"] == 30


async def test_retries_then_succeeds(config_file, patch_litellm):
    def handler(kwargs, n):
        if n == 1:
            raise RuntimeError("connection reset")
        return fake_response("second time lucky")

    calls = patch_litellm(handler)
    gateway = LLMGateway(config_file)

    response = await gateway.complete("reflection", [{"role": "user", "content": "hi"}])

    assert response.content == "second time lucky"
    assert len(calls) == 2


async def test_falls_back_to_next_profile(config_file, patch_litellm):
    def handler(kwargs, n):
        if kwargs["model"].startswith("hosted_vllm"):
            raise RuntimeError("vllm pod is down")
        return fake_response("from the fallback")

    calls = patch_litellm(handler)
    gateway = LLMGateway(config_file)

    response = await gateway.complete("reflection", [{"role": "user", "content": "hi"}])

    assert response.profile == "anthropic"
    # Both attempts against the primary are spent before the chain moves on.
    assert [c["model"] for c in calls] == [
        "hosted_vllm/llama",
        "hosted_vllm/llama",
        "claude-sonnet-5",
    ]


async def test_raises_when_every_profile_fails(config_file, patch_litellm):
    def handler(kwargs, n):
        raise RuntimeError("nothing is reachable")

    patch_litellm(handler)
    gateway = LLMGateway(config_file)

    with pytest.raises(LLMError, match="all LLM profiles failed"):
        await gateway.complete("reflection", [{"role": "user", "content": "hi"}])


# -- structured output ------------------------------------------------------


class Plan(BaseModel):
    hypotheses: list[str]


async def test_complete_json_validates(config_file, patch_litellm):
    patch_litellm(lambda kwargs, n: fake_response(json.dumps({"hypotheses": ["disk full"]})))
    gateway = LLMGateway(config_file)

    plan = await gateway.complete_json("planner", [{"role": "user", "content": "x"}], Plan)

    assert plan.hypotheses == ["disk full"]


async def test_complete_json_repairs_a_bad_first_answer(config_file, patch_litellm):
    def handler(kwargs, n):
        if n == 1:
            return fake_response("Sure! Here is the plan: it is probably the disk.")
        return fake_response(json.dumps({"hypotheses": ["disk full"]}))

    calls = patch_litellm(handler)
    gateway = LLMGateway(config_file)

    plan = await gateway.complete_json("planner", [{"role": "user", "content": "x"}], Plan)

    assert plan.hypotheses == ["disk full"]
    # The repair turn shows the model its own bad output plus the reason it was rejected.
    assert len(calls[1]["messages"]) > len(calls[0]["messages"])


async def test_complete_json_gives_up_after_the_repair_turn(config_file, patch_litellm):
    patch_litellm(lambda kwargs, n: fake_response("still not json"))
    gateway = LLMGateway(config_file)

    with pytest.raises(LLMError, match="unusable JSON"):
        await gateway.complete_json("planner", [{"role": "user", "content": "x"}], Plan)


# -- json extraction --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Here you go:\n{"a": 1}\nHope that helps!',
    ],
)
def test_extract_json_tolerates_model_packaging(text):
    assert extract_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "   ", "no json at all"])
def test_extract_json_rejects_junk(text):
    with pytest.raises(ValueError):
        extract_json(text)


# -- structured output mode -------------------------------------------------


def _cfg(tmp_path, **profile_extra):
    config = {
        "default_profile": "vllm",
        "defaults": {"max_attempts": 2, "retry_backoff_seconds": 0},
        "profiles": {
            "vllm": {
                "model": "hosted_vllm/qwen",
                "api_base": "http://vllm:8000/v1",
                **profile_extra,
            }
        },
    }
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


async def test_response_format_is_sent_by_default(tmp_path, patch_litellm):
    calls = patch_litellm(lambda kwargs, n: fake_response('{"a": 1}'))
    gateway = LLMGateway(_cfg(tmp_path))

    await gateway.complete("planner", [{"role": "user", "content": "x"}], json_mode=True)

    assert calls[0]["response_format"] == {"type": "json_object"}


async def test_prompt_mode_instructs_instead_of_using_response_format(tmp_path, patch_litellm):
    """vLLM turns response_format into guided decoding; prompt mode must avoid the field."""
    calls = patch_litellm(lambda kwargs, n: fake_response('{"a": 1}'))
    gateway = LLMGateway(_cfg(tmp_path, structured_output="prompt"))

    await gateway.complete(
        "planner",
        [{"role": "system", "content": "You plan."}, {"role": "user", "content": "x"}],
        json_mode=True,
    )

    assert "response_format" not in calls[0]
    system = [m for m in calls[0]["messages"] if m["role"] == "system"]
    assert len(system) == 1, "a second system turn breaks Llama/Qwen chat templates"
    assert "valid JSON object only" in system[0]["content"]
    assert "You plan." in system[0]["content"]


async def test_prompt_mode_inserts_a_system_turn_when_there_is_none(tmp_path, patch_litellm):
    calls = patch_litellm(lambda kwargs, n: fake_response('{"a": 1}'))
    gateway = LLMGateway(_cfg(tmp_path, structured_output="prompt"))

    await gateway.complete("planner", [{"role": "user", "content": "x"}], json_mode=True)

    assert calls[0]["messages"][0]["role"] == "system"


async def test_prompt_mode_does_not_mutate_the_caller_s_messages(tmp_path, patch_litellm):
    """The caller reuses its list across retries and the repair turn; mutating it compounds."""
    patch_litellm(lambda kwargs, n: fake_response('{"a": 1}'))
    gateway = LLMGateway(_cfg(tmp_path, structured_output="prompt"))
    messages = [{"role": "system", "content": "You plan."}, {"role": "user", "content": "x"}]

    await gateway.complete("planner", messages, json_mode=True)
    await gateway.complete("planner", messages, json_mode=True)

    assert messages == [
        {"role": "system", "content": "You plan."},
        {"role": "user", "content": "x"},
    ]


async def test_json_mode_off_adds_no_instruction(tmp_path, patch_litellm):
    calls = patch_litellm(lambda kwargs, n: fake_response("hello"))
    gateway = LLMGateway(_cfg(tmp_path, structured_output="prompt"))

    await gateway.complete("planner", [{"role": "user", "content": "x"}])

    assert calls[0]["messages"] == [{"role": "user", "content": "x"}]
    assert "response_format" not in calls[0]


async def test_a_server_rejecting_guided_decoding_downgrades_itself(tmp_path, patch_litellm):
    def handler(kwargs, n):
        if "response_format" in kwargs:
            raise RuntimeError(
                "litellm.BadRequestError: guided decoding backend xgrammar failed to initialise"
            )
        return fake_response('{"a": 1}')

    calls = patch_litellm(handler)
    gateway = LLMGateway(_cfg(tmp_path))

    response = await gateway.complete(
        "planner", [{"role": "user", "content": "x"}], json_mode=True
    )

    assert response.content == '{"a": 1}'
    assert len(calls) == 2, "should recover on the spot, not burn the retry budget"
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    # The downgrade sticks, so the next call does not repeat the failure.
    assert gateway.profiles["vllm"].structured_output == "prompt"


async def test_downgrade_does_not_consume_the_retry_budget(tmp_path, patch_litellm):
    """After downgrading, a genuinely flaky server still gets its full allowance."""
    def handler(kwargs, n):
        if "response_format" in kwargs:
            raise RuntimeError("guided decoding unsupported")
        if n == 2:
            raise RuntimeError("connection reset")
        return fake_response('{"a": 1}')

    calls = patch_litellm(handler)
    gateway = LLMGateway(_cfg(tmp_path))

    response = await gateway.complete(
        "planner", [{"role": "user", "content": "x"}], json_mode=True
    )

    assert response.content == '{"a": 1}'
    assert len(calls) == 3


async def test_an_ordinary_error_does_not_trigger_a_downgrade(tmp_path, patch_litellm):
    patch_litellm(lambda kwargs, n: (_ for _ in ()).throw(RuntimeError("connection refused")))
    gateway = LLMGateway(_cfg(tmp_path))

    with pytest.raises(LLMError):
        await gateway.complete("planner", [{"role": "user", "content": "x"}], json_mode=True)

    assert gateway.profiles["vllm"].structured_output == "response_format"
