from tartarus.manifest import Capability, Grant
from tartarus.policy import PolicyEngine


def _capability(policy, name="cap"):
    return Capability(
        name=name,
        description="desc",
        policy=policy,
        params={},
        grants=Grant(),
        runner="true",
    )


def _always_yes():
    calls = []

    def prompt(*args):
        calls.append(args)
        return True

    return prompt, calls


def test_auto_allows_without_prompting():
    prompt, calls = _always_yes()
    engine = PolicyEngine(prompt=prompt)

    decision = engine.decide(_capability("auto"), {}, "true")

    assert decision.allowed
    assert calls == []


def test_deny_never_allows_even_with_approval():
    prompt, _ = _always_yes()
    engine = PolicyEngine(prompt=prompt)

    decision = engine.decide(_capability("deny"), {}, "true")

    assert not decision.allowed


def test_ask_always_prompts_every_call():
    prompt, calls = _always_yes()
    engine = PolicyEngine(prompt=prompt)
    capability = _capability("ask-always")

    engine.decide(capability, {}, "true")
    engine.decide(capability, {}, "true")

    assert len(calls) == 2


def test_ask_always_decline_is_denied():
    engine = PolicyEngine(prompt=lambda *_: False)

    decision = engine.decide(_capability("ask-always"), {}, "true")

    assert not decision.allowed
    assert "declined" in decision.reason


def test_ask_once_remembers_within_session():
    prompt, calls = _always_yes()
    engine = PolicyEngine(prompt=prompt)
    capability = _capability("ask-once", name="write")

    first = engine.decide(capability, {}, "true")
    second = engine.decide(capability, {}, "true")

    assert first.allowed and second.allowed
    assert len(calls) == 1  # prompted only the first time
    assert second.approver == "session"


def test_ask_once_decline_is_not_remembered():
    seen = []

    def prompt(*_):
        seen.append(1)
        return False

    engine = PolicyEngine(prompt=prompt)
    capability = _capability("ask-once", name="write")

    engine.decide(capability, {}, "true")
    engine.decide(capability, {}, "true")

    assert len(seen) == 2  # asked again because the first was declined


def test_headless_denies_ask_policies_but_allows_auto():
    engine = PolicyEngine(headless=True, prompt=lambda *_: True)

    assert not engine.decide(_capability("ask-once"), {}, "true").allowed
    assert not engine.decide(_capability("ask-always"), {}, "true").allowed
    assert engine.decide(_capability("auto"), {}, "true").allowed
