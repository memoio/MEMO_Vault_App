"""Parse a restricted JSON action without executing model-generated Python."""
import ast
import json


PARAMETERS = {'click': ('bid',), 'fill': ('bid', 'value'), 'select_option': ('bid', 'options')}


def parse_agent_action(response, observed_bids):
    payload = json.loads(response)
    if not isinstance(payload, dict) or not isinstance(payload.get('action'), str):
        raise ValueError('expected JSON object with action string')
    try:
        node = ast.parse(payload['action'], mode='eval').body
    except SyntaxError as exc:
        raise ValueError('invalid action syntax') from exc
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in PARAMETERS:
        raise ValueError('unsupported action')
    names = PARAMETERS[node.func.id]
    if len(node.args) > len(names):
        raise ValueError('too many arguments')
    values = {}
    for name, arg in zip(names, node.args):
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            raise ValueError('arguments must be literal strings')
        values[name] = arg.value
    for arg in node.keywords:
        if arg.arg not in names or arg.arg in values or not isinstance(arg.value, ast.Constant) or not isinstance(arg.value.value, str):
            raise ValueError('invalid or duplicate argument')
        values[arg.arg] = arg.value.value
    if set(values) != set(names):
        raise ValueError('missing required argument')
    if not values['bid'] or values['bid'] not in observed_bids:
        raise ValueError('bid is not observed in current page')
    return f"{node.func.id}({', '.join(repr(values[name]) for name in names)})"
