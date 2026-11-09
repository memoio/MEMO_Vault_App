import json

import pytest

from everos.agent_training.paper_adp_conversion import (
    convert_codeact,
    convert_mind2web,
    convert_swe,
    validate_paper_trajectory,
)


def test_codeact_uses_code_action_and_keeps_long_observation():
    row = {
        "id": "code1",
        "conversations": [
            {"role": "user", "content": "run code"},
            {"role": "assistant", "content": "thinking\n<execute>print(1)</execute>"},
            {"role": "user", "content": "Observation:\n" + "x" * 10000},
            {"role": "assistant", "content": "<solution>1</solution>"},
        ],
    }
    t = convert_codeact(row)
    assert t["content"][1] == {
        "class_": "code_action",
        "language": "python",
        "content": "print(1)",
        "description": "thinking",
    }
    assert len(t["content"][2]["content"]) > 8000
    validate_paper_trajectory(t)


def test_mind2web_uses_full_raw_html_and_preserves_unknown_fields():
    html = '<html backend_node_id="7">' + "x" * 9000 + "</html>"
    row = {
        "annotation_id": "m1",
        "confirmed_task": "click",
        "website": "example",
        "actions": [
            {
                "action_uid": "a",
                "raw_html": html,
                "operation": {"op": "CLICK", "value": ""},
                "pos_candidates": [
                    {"backend_node_id": "7", "is_original_target": True}
                ],
            }
        ],
    }
    t = convert_mind2web(row)
    assert t["content"][1]["class_"] == "web_observation"
    assert t["content"][1]["html"] == html
    assert t["content"][1]["url"] is None and t["content"][1]["axtree"] is None
    assert t["content"][2]["class_"] == "api_action"
    validate_paper_trajectory(t)


def test_swe_bash_maps_to_code_and_terminal_submit_is_not_fabricated():
    row = {
        "traj_id": "s1",
        "instance_id": "repo.x",
        "resolved": True,
        "model": "m",
        "messages": json.dumps(
            [
                {"role": "user", "content": "fix"},
                {
                    "role": "assistant",
                    "content": "look",
                    "tool_calls": [
                        {
                            "id": "a",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command":"ls"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "content": "file", "tool_call_ids": ["a"]},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "b", "function": {"name": "submit", "arguments": "{}"}}
                    ],
                },
            ]
        ),
    }
    t = convert_swe(row)
    assert (
        t["content"][1]["class_"] == "code_action"
        and t["content"][1]["language"] == "bash"
    )
    assert t["content"][-1]["function"] == "submit"
    validate_paper_trajectory(t)


def test_invalid_mind2web_target_is_not_invented():
    row = {
        "annotation_id": "x",
        "confirmed_task": "click",
        "actions": [
            {"raw_html": "x", "operation": {"op": "CLICK"}, "pos_candidates": []}
        ],
    }
    with pytest.raises(ValueError):
        convert_mind2web(row)


def test_agentinstruct_bash_uses_code_action_and_preserves_thought():
    from everos.agent_training.paper_adp_conversion import convert_agentinstruct

    row = {
        "id": "os_1",
        "conversations": [
            {"from": "human", "value": "list files", "loss": None},
            {
                "from": "gpt",
                "value": "Think: list directory\n\nAct: bash\n\n```bash\nls\n```",
                "loss": True,
            },
            {"from": "human", "value": "file.txt", "loss": None},
        ],
    }
    t = convert_agentinstruct(row)
    assert t["content"][1]["class_"] == "code_action"
    assert t["content"][1]["language"] == "bash"
    assert "list directory" in t["content"][1]["description"]
    assert t["content"][2]["source"] == "environment"
    validate_paper_trajectory(t)
