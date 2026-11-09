from everos.agent_training.prepare_datasets import (
    assign_split,
    convert_codeact,
    convert_swe,
    convert_web,
    dataset_group,
)


def test_task_group_is_stable_and_repository_level():
    left = {"instance_id": "django-money__django-money.aaaa.func_one", "traj_id": "one"}
    right = {
        "instance_id": "django-money__django-money.bbbb.func_two",
        "traj_id": "two",
    }
    assert dataset_group("swe_smith", left) == dataset_group("swe_smith", right)
    assert assign_split(
        "swe_smith", dataset_group("swe_smith", left), 42
    ) == assign_split("swe_smith", dataset_group("swe_smith", right), 42)


def test_codeact_separates_tool_output_from_user_message():
    record = {
        "id": "task/1",
        "conversations": [
            {"role": "user", "content": "Calculate 1 + 1"},
            {"role": "assistant", "content": "<execute>print(1 + 1)</execute>"},
            {"role": "user", "content": "Observation:\n2"},
            {"role": "assistant", "content": "<solution>2</solution>"},
        ],
    }
    t = convert_codeact(record)
    assert [(x["class_"], x.get("source")) for x in t["content"]] == [
        ("text_observation", "user"),
        ("api_action", None),
        ("text_observation", "environment"),
        ("message_action", None),
    ]
    assert t["content"][1]["kwargs"]["code"] == "print(1 + 1)"


def test_swe_pairs_tool_calls_and_preserves_error():
    record = {
        "traj_id": "t1",
        "instance_id": "repo__project.id",
        "resolved": True,
        "messages": [
            {"role": "user", "content": "fix"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "bash", "arguments": {"command": "ls"}},
                    }
                ],
            },
            {
                "role": "tool",
                "content": "oops",
                "tool_call_ids": ["c1"],
                "is_error": True,
            },
        ],
    }
    t = convert_swe(record)
    assert t["content"][1]["function"] == "bash"
    assert t["details"]["tool_links"][0]["call_id"] == "c1"
    assert t["details"]["tool_links"][0]["is_error"] is True


def test_web_truncation_is_explicit_and_not_training_ready():
    r = {
        "annotation_id": "web1",
        "website": "site",
        "confirmed_task": "find item",
        "actions": [
            {
                "action_uid": "a",
                "cleaned_html": "abcdef",
                "operation": {"op": "CLICK", "value": ""},
                "pos_candidates": [
                    {"backend_node_id": "123", "is_original_target": True}
                ],
            }
        ],
    }
    t = convert_web(r, max_observation_chars=3)
    assert t["details"]["conversion"]["lossy"] is True
    assert t["details"]["conversion"]["training_ready"] is False
    assert t["content"][1]["content"] == "abc"
    assert t["content"][2]["kwargs"]["backend_node_id"] == "123"


def test_swe_terminal_submit_is_retained_without_inventing_result():
    import json

    row = {
        "traj_id": "x",
        "instance_id": "repo__name.a",
        "resolved": True,
        "messages": json.dumps(
            [
                {"role": "user", "content": "fix the bug"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "end", "function": {"name": "submit", "arguments": "{}"}}
                    ],
                },
            ]
        ),
    }
    result = convert_swe(row)
    assert result["content"][-1]["function"] == "submit"
    assert result["details"]["tool_links"][0]["terminal"] is True
    assert "observation_index" not in result["details"]["tool_links"][0]


def test_codeact_associates_result_to_action():
    row = {
        "id": "one",
        "conversations": [
            {"role": "user", "content": "calculate"},
            {"role": "assistant", "content": "<execute>print(2)</execute>"},
            {"role": "user", "content": "Observation: 2"},
        ],
    }
    link = convert_codeact(row)["details"]["tool_links"][0]
    assert link["action_index"] == 1
    assert link["observation_index"] == 2
