"""Fork-owned policy tests for self-contained Clarify decision cards."""

from copy import deepcopy

from fork_features import clarify_decision_card


class TestClarifyDecisionCardPolicy:
    def test_legacy_schema_extension_is_pure_and_preserves_shape(self):
        base = {
            "name": "clarify",
            "description": "Base clarify guidance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Base question."},
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Base choices.",
                    },
                    "multi_select": {"type": "boolean"},
                },
                "required": ["question"],
            },
        }
        original = deepcopy(base)

        extended = clarify_decision_card.apply_decision_card_policy(base)

        assert base == original
        assert extended is not base
        assert extended["parameters"]["required"] == ["question"]
        assert set(extended["parameters"]["properties"]) == {
            "question",
            "choices",
            "multi_select",
        }
        assert "self-contained" in extended["description"].lower()
        assert "one user decision per card" in extended["description"].lower()
        question = extended["parameters"]["properties"]["question"]["description"]
        choices = extended["parameters"]["properties"]["choices"]["description"]
        assert "current situation" in question.lower()
        assert "each choice must stand alone" in choices.lower()

    def test_policy_can_extend_upstream_batch_shape_without_changing_api(self):
        base = {
            "name": "clarify",
            "description": "Batch clarify guidance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": "Base questions.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "choices": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "multi_select": {"type": "boolean"},
                            },
                            "required": ["question"],
                        },
                    }
                },
                "required": ["questions"],
            },
        }
        original = deepcopy(base)

        extended = clarify_decision_card.apply_decision_card_policy(base)

        assert base == original
        assert extended["parameters"]["required"] == ["questions"]
        questions = extended["parameters"]["properties"]["questions"]
        item_properties = questions["items"]["properties"]
        assert "self-contained" in questions["description"].lower()
        assert "current situation" in item_properties["question"]["description"].lower()
        assert "each choice must stand alone" in item_properties["choices"]["description"].lower()
