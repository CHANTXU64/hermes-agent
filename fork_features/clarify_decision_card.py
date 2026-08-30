"""Fork-owned model guidance for self-contained Clarify decision cards.

The Clarify tool owns its API shape, validation, callback, and response format.
This module owns only the Fork's model-visible decision-card policy and applies
it to a copied schema. It supports the current single-question shape and the
newer upstream ``questions[]`` shape without changing either API.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


DECISION_CARD_GUIDANCE = (
    "SELF-CONTAINED PROMPT: the clarify UI may render this as a standalone "
    "card without any other assistant prose from the tool-call turn. Put all "
    "context the user needs to decide in `question`. For an action or approval "
    "decision, briefly explain the current situation, proposed action and "
    "scope, material impact or trade-off, and your recommendation when you "
    "have one. Do not use references such as 'above', 'earlier', 'the "
    "recommended scope', or 'as discussed' as substitutes for that context; "
    "the user must be able to answer from the card alone.\n\n"
    "DECISION-FIRST AND CONCISE: ask one user decision per card. If the user's "
    "goal or overall approach is not confirmed, ask that before asking about "
    "implementation scope. Use plain language, usually 2-5 short sentences. "
    "For approval, name the complete plan, state what will actually change, "
    "its scope and material impacts, give a recommendation when useful, then "
    "ask whether to execute that named plan. A local path, plan document, link, "
    "internal task number, or prior prose never substitutes for this explanation. "
    "The user's choice authorizes only the scope stated in the card; material "
    "scope added later requires a new clarification."
)

QUESTION_DESCRIPTION = (
    "The complete, self-contained prompt shown to the user. Include "
    "all context needed to answer without relying on earlier "
    "assistant prose. For an action or approval decision, briefly "
    "state the current situation and one decision in plain language, "
    "usually in 2-5 short sentences. Name the complete plan, proposed "
    "action and scope, material impact or trade-off, and your recommendation "
    "when one exists, then ask whether to execute that named plan. Do not rely "
    "on a local path, plan document, link, internal task number, or "
    "later-added scope. Do not embed the answer options here — pass "
    "them as separate elements in `choices`."
)

CHOICES_DESCRIPTION = (
    "REQUIRED whenever you are presenting selectable options: "
    "each distinct option is its own array element (up to 4). "
    "Each choice must stand alone and name the complete action or "
    "plan; never use labels such as 'Task 1-3', 'the recommended "
    "scope', or 'the plan above' as substitutes for the action. "
    "The UI renders these as pickable rows and auto-appends an "
    "'Other (type your answer)' option. Omit this parameter "
    "entirely ONLY for a genuinely open-ended free-text question."
)

CHOICE_STANDALONE_GUIDANCE = (
    "Each choice must stand alone and name the complete action or plan; never "
    "use labels such as 'Task 1-3', 'the recommended scope', or 'the plan above' "
    "as substitutes for the action."
)


def _append_text(existing: Any, addition: str) -> str:
    text = str(existing or "").strip()
    if addition in text:
        return text
    return f"{text}\n\n{addition}" if text else addition


def _extend_tool_description(description: Any) -> str:
    text = str(description or "")
    if DECISION_CARD_GUIDANCE in text:
        return text
    marker = "CRITICAL: when you are offering options"
    if marker in text:
        return text.replace(
            marker,
            f"{DECISION_CARD_GUIDANCE}\n\n{marker}",
            1,
        )
    return _append_text(text, DECISION_CARD_GUIDANCE)


def apply_decision_card_policy(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copied Clarify schema with the Fork decision-card guidance.

    The current single-question schema keeps its exact pre-refactor rendered
    text. The newer upstream batch schema is extended additively so its
    ``questions`` API, recommendation semantics, and required fields remain
    owned by upstream.
    """
    extended = deepcopy(schema)
    extended["description"] = _extend_tool_description(extended.get("description"))

    parameters = extended.get("parameters")
    if not isinstance(parameters, dict):
        return extended
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return extended

    question = properties.get("question")
    if isinstance(question, dict):
        question["description"] = QUESTION_DESCRIPTION
        choices = properties.get("choices")
        if isinstance(choices, dict):
            choices["description"] = CHOICES_DESCRIPTION
        return extended

    questions = properties.get("questions")
    if not isinstance(questions, dict):
        return extended
    questions["description"] = _append_text(
        questions.get("description"),
        "Each rendered question must be a complete, self-contained decision card.",
    )
    items = questions.get("items")
    if not isinstance(items, dict):
        return extended
    item_properties = items.get("properties")
    if not isinstance(item_properties, dict):
        return extended

    item_question = item_properties.get("question")
    if isinstance(item_question, dict):
        item_question["description"] = _append_text(
            item_question.get("description"),
            QUESTION_DESCRIPTION,
        )
    item_choices = item_properties.get("choices")
    if isinstance(item_choices, dict):
        item_choices["description"] = _append_text(
            item_choices.get("description"),
            CHOICE_STANDALONE_GUIDANCE,
        )
    return extended
