from everos.agent_training.audit_stage1 import audit_record, cross_split_conflicts


def test_web_last_action_is_not_treated_as_observed_result():
    record = {'id': 'a', 'details': {'source_json': '{"dataset":"mind2web", "group":"site"}'}, 'content': [
        {'class_': 'text_observation', 'source': 'user', 'content': 'task'},
        {'class_': 'web_observation', 'html': '<div backend_node_id="1">'},
        {'class_': 'api_action', 'function': 'click', 'kwargs': {'backend_node_id': '1'}}]}
    result = audit_record(record)
    assert result['unobserved_actions'] == 1
    assert result['tool_evidence'] == 'upstream_declaration_requires_parameter_mapping'


def test_codeact_marks_embedded_tool_definition():
    record = {'id': 'a', 'details': {'source_json': '{"dataset":"codeactinstruct", "group":"g"}', 'system_messages_json': '["Tool functions available: wikipedia_search(query)"]'}, 'content': [
        {'class_': 'text_observation', 'source': 'user', 'content': 'task'},
        {'class_': 'code_action', 'language': 'python', 'content': 'print(1)'},
        {'class_': 'text_observation', 'source': 'environment', 'content': '1'}]}
    result = audit_record(record)
    assert result['unobserved_actions'] == 0
    assert result['has_system']


def test_split_conflict_detects_group_reuse_across_partitions():
    records = [{'dataset':'x', 'group':'g', 'partition':'train/general'}, {'dataset':'x', 'group':'g', 'partition':'validation/validation'}]
    assert cross_split_conflicts(records, 'group') == [{'dataset': 'x', 'value': 'g', 'partitions': ['train/general', 'validation/validation']}]
