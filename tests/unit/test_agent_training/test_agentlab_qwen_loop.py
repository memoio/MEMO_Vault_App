import os

import pytest

from everos.agent_training.agentlab_qwen_loop import (
    build_sft_user_prompt,
    confirm_bid_from_observation,
    ensure_loopback_no_proxy,
    is_expected_call,
    parse_agentlab_sft_action,
    validate_browser_action,
)


def observation(*nodes):
    return {"axtree_object": {"nodes": list(nodes)}}


def node(role, name, bid):
    return {
        "role": {"value": role},
        "name": {"value": name},
        "browsergym_id": bid,
    }


def test_confirm_bid_is_selected_by_unique_role_name_and_visible_id():
    obs = observation(node("button", "Cancel", "a1"), node("button", "Confirm", "a2"))
    assert confirm_bid_from_observation(obs) == "a2"


def test_confirm_bid_rejects_missing_or_ambiguous_element():
    with pytest.raises(ValueError):
        confirm_bid_from_observation(observation(node("button", "Cancel", "a1")))
    with pytest.raises(ValueError):
        confirm_bid_from_observation(
            observation(node("button", "Confirm", "a1"), node("button", "Confirm", "a2"))
        )


def test_browser_action_is_restricted_to_one_call_and_current_observed_bids():
    assert validate_browser_action("click('a2')", {"a2"}) == "click('a2')"
    assert validate_browser_action("fill('a2', 'Ada')", {"a2"}) == "fill('a2', 'Ada')"
    with pytest.raises(ValueError):
        validate_browser_action("click('missing')", {"a2"})
    with pytest.raises(ValueError):
        validate_browser_action("__import__('os').system('id')", {"a2"})
    with pytest.raises(ValueError):
        validate_browser_action("click('a2'); print('extra')", {"a2"})


def test_expected_call_helper_does_not_execute_source():
    assert is_expected_call("click('a2')", "click", "a2")
    assert not is_expected_call("click('wrong')", "click", "a2")
    assert not is_expected_call("__import__('os').system('id')", "click", "a2")


def test_adp_json_action_protocol_matches_training_targets_and_observed_bids():
    import json

    assert parse_agentlab_sft_action(
        json.dumps({"thought": "", "action": 'click(bid="a2")'}), {"a2"}
    ) == "click('a2')"
    with pytest.raises(ValueError):
        parse_agentlab_sft_action(json.dumps({"action": 'click(bid="missing")'}), {"a2"})
    with pytest.raises(ValueError):
        parse_agentlab_sft_action("```json\n{\"action\": \"click(bid=\\\"a2\\\")\"}\n```", {"a2"})


def test_sft_loop_prompt_uses_the_published_json_action_response_contract():
    prompt = build_sft_user_prompt("Click Confirm", "click(bid: str)", "[a2] button 'Confirm'")
    assert "# Goal\nClick Confirm" in prompt
    assert "# Current page Accessibility Tree\n[a2] button 'Confirm'" in prompt
    assert 'JSON object with "thought" and "action"' in prompt


def test_loopback_api_hosts_are_added_to_both_proxy_bypass_variables(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.org")
    monkeypatch.delenv("no_proxy", raising=False)
    ensure_loopback_no_proxy()
    assert set(os.environ["NO_PROXY"].split(",")) == {
        "example.org", "127.0.0.1", "localhost"
    }
    assert set(os.environ["no_proxy"].split(",")) == {
        "127.0.0.1", "localhost"
    }
