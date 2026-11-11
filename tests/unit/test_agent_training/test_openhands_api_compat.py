from everos.agent_training.openhands_api_compat import translate_completion_request


def test_maps_openhands_completion_limit_to_llamafactory_max_tokens():
    request = {
        'model': 'qwen35',
        'messages': [{'role': 'user', 'content': 'hello'}],
        'max_completion_tokens': 64,
        'temperature': 0,
    }

    translated = translate_completion_request(request)

    assert translated['max_tokens'] == 64
    assert 'max_completion_tokens' not in translated
    assert request['max_completion_tokens'] == 64


def test_existing_max_tokens_takes_precedence_and_unknown_fields_survive():
    translated = translate_completion_request({
        'model': 'qwen35', 'max_tokens': 32, 'max_completion_tokens': 64,
        'seed': 42,
    })

    assert translated == {'model': 'qwen35', 'max_tokens': 32, 'seed': 42}
