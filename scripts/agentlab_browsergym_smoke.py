"""Run fixed, model-free BrowserGym actions on an isolated in-memory page."""
import json
from urllib.parse import quote

import browsergym.core  # registers openended
import gymnasium as gym
from everos.agent_training.agentlab_action import parse_agent_action

HTML = '''<!doctype html><html><body>
<label for="name">Name</label><input id="name" aria-label="Name">
<button onclick="document.getElementById('result').textContent='Hello '+document.getElementById('name').value">Submit</button>
<p id="result" role="status">Waiting</p></body></html>'''


def find_bid(obs, role, name):
    nodes = [n for n in obs['axtree_object']['nodes']
             if n.get('role', {}).get('value') == role
             and n.get('name', {}).get('value') == name and n.get('browsergym_id')]
    if len(nodes) != 1:
        raise ValueError(f'expected one {role}/{name}, got {len(nodes)}')
    return nodes[0]['browsergym_id']


def main():
    env = gym.make('browsergym/openended', task_kwargs={'start_url':'data:text/html,'+quote(HTML)},
                   headless=True, pre_observation_delay=0.1)
    try:
        obs, _ = env.reset(seed=42)
        input_bid = find_bid(obs, 'textbox', 'Name')
        command = parse_agent_action(json.dumps({'action': f'fill({input_bid!r}, {"Ada"!r})'}), {input_bid})
        obs, _, _, _, _ = env.step(command)
        if obs['last_action_error']:
            raise RuntimeError(obs['last_action_error'])
        button_bid = find_bid(obs, 'button', 'Submit')
        command = parse_agent_action(json.dumps({'action': f'click({button_bid!r})'}), {button_bid})
        obs, _, _, _, _ = env.step(command)
        if obs['last_action_error']:
            raise RuntimeError(obs['last_action_error'])
        text = [n.get('name', {}).get('value', '') for n in obs['axtree_object']['nodes']]
        if 'Hello Ada' not in text:
            raise ValueError('expected updated accessible text after click')
        print(json.dumps({'ok':True, 'input_bid':input_bid, 'button_bid':button_bid,
                          'result':'Hello Ada', 'mode':'preset JSON actions via parser; no agent model'}))
    finally:
        env.close()


if __name__ == '__main__':
    main()
