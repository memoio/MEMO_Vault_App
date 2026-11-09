from everos.agent_training.audit_tool_interfaces import inspect_swe_link, inspect_web_action


def test_swe_link_requires_observation_or_terminal_submit():
    content = [{'class_':'code_action','language':'bash','content':'ls'}]
    result = inspect_swe_link(content, {'action_index':0,'source_kwargs':{'command':'ls'}})
    assert 'missing_observation' in result['issues']


def test_swe_bash_argument_must_match_recorded_action():
    content = [{'class_':'code_action','language':'bash','content':'ls'}, {'class_':'text_observation','source':'environment','content':'ok'}]
    result = inspect_swe_link(content, {'action_index':0,'source_kwargs':{'command':'pwd'},'observation_index':1})
    assert 'action_arguments_mismatch' in result['issues']


def test_web_id_must_exist_in_preceding_snapshot():
    content = [{'class_':'web_observation','html':'<button backend_node_id="1">OK</button>'}, {'class_':'api_action','function':'click','kwargs':{'backend_node_id':'2','value':''}}]
    result = inspect_web_action(content, 1)
    assert result['target_matches'] == 0
    assert 'target_not_unique_in_html' in result['issues']


def test_web_id_match_is_not_xpath_mapping():
    content = [{'class_':'web_observation','html':'<button backend_node_id="1">OK</button>'}, {'class_':'api_action','function':'click','kwargs':{'backend_node_id':'1','value':''}}]
    result = inspect_web_action(content, 1)
    assert result['target_matches'] == 1
    assert result['xpath_mapping_verified'] is False
