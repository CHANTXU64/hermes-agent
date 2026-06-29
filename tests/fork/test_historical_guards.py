"""Fork-owned historical regression guards for reverted local behavior."""

import run_agent as run_agent_module
from run_agent import AIAgent
from tests.run_agent.test_background_review import (
    ImmediateThread,
    _bare_agent,
)


def test_background_review_uses_class_prompt_not_configured_instance_prompt(monkeypatch):
    """Custom review prompts from config are disabled; use class prompts only."""
    captured = {}

    class FakeReviewAgent:
        def __init__(self, **kwargs):
            self._session_messages = []

        def run_conversation(self, **kwargs):
            captured["user_message"] = kwargs["user_message"]

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_agent_module, "AIAgent", FakeReviewAgent)
    monkeypatch.setattr(run_agent_module.threading, "Thread", ImmediateThread)

    agent = _bare_agent()
    agent._skill_review_prompt = "custom configured skill prompt"

    AIAgent._spawn_background_review(
        agent,
        messages_snapshot=[{"role": "user", "content": "hello"}],
        review_skills=True,
    )

    assert captured["user_message"].startswith("review skills")
    assert "custom configured skill prompt" not in captured["user_message"]
