import json
import pytest
from everos.agent_training.agentlab_action import parse_agent_action


def test_json_click_and_fill_use_observed_bid():
    assert parse_agent_action(json.dumps({'thought':'', 'action':'click(bid="7")'}), {'7'}) == "click('7')"
    assert parse_agent_action(json.dumps({'thought':'', 'action':'fill("7", "Ada")'}), {'7'}) == "fill('7', 'Ada')"


def test_rejects_missing_bid_and_unrecognized_code():
    with pytest.raises(ValueError, match='observed'):
        parse_agent_action(json.dumps({'action':'click("9")'}), {'7'})
    with pytest.raises(ValueError):
        parse_agent_action(json.dumps({'action':'__import__("os")'}), {'7'})
    with pytest.raises(ValueError):
        parse_agent_action(json.dumps({'action':'type(bid="7", value="a")'}), {'7'})
