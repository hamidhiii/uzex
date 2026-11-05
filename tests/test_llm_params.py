"""Проверка сборки LLM-запроса без сети: кэш промпта, учёт стоимости, автоотключение неподдерживаемых параметров."""
import types

import httpx
import pytest
from openai import BadRequestError

from app import agent, config


class FakeCompletions:
    def __init__(self, reject=()):
        self.calls, self.reject = [], set(reject)

    def create(self, **args):
        self.calls.append(args)
        sysc = args["messages"][0]["content"]
        problems = []
        if "temperature" in self.reject and "temperature" in args:
            problems.append("`temperature` is deprecated for this model.")
        if "tool_choice" in self.reject and args.get("tool_choice") == "required":
            problems.append('tool_choice: type "tool" and "any" are not supported for this model.')
        if "cache" in self.reject and isinstance(sysc, list):
            problems.append("invalid content block")
        if problems:
            req = httpx.Request("POST", "http://x")
            raise BadRequestError(problems[0], response=httpx.Response(400, request=req), body={"message": problems[0]})
        msg = types.SimpleNamespace(content="ok", tool_calls=None)
        usage = types.SimpleNamespace(total_tokens=100, prompt_tokens_details=types.SimpleNamespace(cached_tokens=80),
                                      cost=0.0012, model_extra={})
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)


@pytest.fixture
def fake(monkeypatch):
    def make(reject=(), base="https://openrouter.ai/api/v1"):
        comp = FakeCompletions(reject)
        cl = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp))
        monkeypatch.setattr(agent, "client", lambda: cl)
        monkeypatch.setattr(config, "LLM_BASE_URL", base)
        monkeypatch.setattr(config, "LLM_MODEL", "anthropic/claude-test")
        monkeypatch.setattr(config, "LLM_FALLBACK_MODELS", [])
        monkeypatch.setattr(config, "LLM_PROMPT_CACHE", "auto")
        agent._UNSUPPORTED.clear()
        return comp
    return make


def _call():
    ag = agent.Agent("Сколько сделок по карбамиду в 2025?", "ru")
    msgs = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Q"}]
    ag.call_llm(msgs, agent.TOOLS)
    return ag


def test_openrouter_cache_and_cost(fake):
    comp = fake()
    ag = _call()
    a = comp.calls[-1]
    assert a["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert a["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert a["extra_body"] == {"usage": {"include": True}}
    assert ag.cached_tokens == 80 and abs(ag.cost - 0.0012) < 1e-9


def test_no_cache_for_direct_anthropic(fake):
    comp = fake(base="https://api.anthropic.com/v1/")
    _call()
    a = comp.calls[-1]
    assert isinstance(a["messages"][0]["content"], str) and "extra_body" not in a


def test_auto_drop_unsupported(fake):
    comp = fake(reject=("temperature", "tool_choice", "cache"))
    _call()
    a = comp.calls[-1]
    assert "temperature" not in a and a["tool_choice"] == "auto" and isinstance(a["messages"][0]["content"], str)
    # запомнено: следующий вызов сразу без лишних попыток
    n = len(comp.calls)
    _call()
    assert len(comp.calls) == n + 1


class RateLimited(FakeCompletions):
    """Основная модель всегда отвечает 429, запасная — нормально."""

    def __init__(self, busy):
        super().__init__()
        self.busy = busy

    def create(self, **args):
        if args["model"] == self.busy:
            from openai import RateLimitError
            req = httpx.Request("POST", "http://x")
            self.calls.append(args)
            raise RateLimitError("Rate limit exceeded", response=httpx.Response(429, request=req), body=None)
        return super().create(**args)


def test_429_goes_to_fallback_without_rpm_limit(fake, monkeypatch):
    fake()
    comp = RateLimited("anthropic/claude-test")
    monkeypatch.setattr(agent, "client", lambda: types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp)))
    monkeypatch.setattr(config, "LLM_FALLBACK_MODELS", ["anthropic/claude-backup"])
    monkeypatch.setattr(agent, "LIMITER", agent.RateLimiter(0))  # LLM_RPM=0: пауза после 429 всё равно работает
    ag = _call()
    assert ag.model_used == "anthropic/claude-backup"
    assert [c["model"] for c in comp.calls].count("anthropic/claude-test") == 1


def test_llm_failure_after_resolve_is_error_not_no_data(monkeypatch):
    ag = agent.Agent("2025-yilda sement bo‘yicha eng yirik xaridor kim?", "uz")
    ag.tool_resolve({"kind": "product", "text": "sement"})

    def boom(*a, **k):
        raise RuntimeError("429")
    monkeypatch.setattr(ag, "call_llm", boom)
    r = ag.run()
    assert r["status"] == "error"
