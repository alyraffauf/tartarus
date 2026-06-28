"""PolicyEngine: gate each tool call per its capability's policy (PLAN.md §6.6).

- auto         → allow without asking.
- ask-once     → prompt the first time this session, then remember (keyed by name).
- ask-always   → prompt every invocation.
- deny         → never allow (also never exposed as a tool; defense in depth).

The prompt shows the human the exact delta being requested — the capability, its
grant deltas, and the interpolated command — and requires an explicit y/N,
defaulting to No. In headless mode there is no human, so every ask-* policy denies
(fail closed, PLAN.md §8.4).
"""

import sys
from collections.abc import Callable
from typing import Literal

from pydantic.dataclasses import dataclass

from tartarus.constants import STRICT_CONFIG
from tartarus.manifest import Capability, Grant

# Decides one prompt: (capability, arguments, interpolated command) -> approved?
PromptFn = Callable[[Capability, dict, str], bool]


@dataclass(config=STRICT_CONFIG)
class Decision:
    allowed: bool
    reason: str
    approver: Literal["auto", "deny", "session", "human", "headless", "broker"]


class PolicyEngine:
    def __init__(self, headless: bool = False, prompt: PromptFn | None = None):
        self._headless = headless
        self._prompt = prompt if prompt is not None else prompt_for_approval
        self._session_approved: set[str] = set()

    def decide(self, capability: Capability, arguments: dict, command: str) -> Decision:
        policy = capability.policy
        if policy == "auto":
            return Decision(True, "auto policy", "auto")
        if policy == "deny":
            return Decision(False, "policy is deny", "deny")

        # ask-once / ask-always both need a human.
        if self._headless:
            return Decision(
                False, f"{policy} needs approval but running headless", "headless"
            )
        if policy == "ask-once" and capability.name in self._session_approved:
            return Decision(True, "approved earlier this session", "session")

        if not self._prompt(capability, arguments, command):
            return Decision(False, "declined by human", "human")

        if policy == "ask-once":
            self._session_approved.add(capability.name)
        return Decision(True, "approved by human", "human")


def prompt_for_approval(capability: Capability, arguments: dict, command: str) -> bool:
    """Default prompt: print the requested delta and read an explicit y/N."""
    lines = [
        "",
        f"┌ tool '{capability.name}' requests approval (policy: {capability.policy})",
        f"│ command: {command}",
    ]
    for line in _grant_lines(capability.grants):
        lines.append(f"│ {line}")
    lines.append("└ approve this call?")
    print("\n".join(lines), file=sys.stderr)

    try:
        answer = input("  [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _grant_lines(grant: Grant) -> list[str]:
    lines = []
    if grant.writable:
        lines.append(f"writable: {grant.writable}")
    if grant.allowed_hosts:
        lines.append(f"network: {grant.allowed_hosts}")
    if grant.package_bins:
        lines.append(f"packageBins: {grant.package_bins}")
    if grant.unrestricted:
        lines.append("UNRESTRICTED escape — full host reach")
    if not lines:
        lines.append("grants: none beyond the shell (read-only work tree, no network)")
    return lines
