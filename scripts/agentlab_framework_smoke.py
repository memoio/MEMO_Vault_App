"""Exercise AgentLab MostBasicAgent -> BrowserGym with a scripted chat model."""
import json
from dataclasses import dataclass
from urllib.parse import quote
import gymnasium as gym
import browsergym.core
from browsergym.utils.obs import flatten_dom_to_str, prune_html
from agentlab.agents.most_basic_agent.most_basic_agent import MostBasicAgent
from agentlab.llm.llm_utils import AIMessage

HTML = '''<html><body><label for="name">Name</label><input id="name" aria-label="Name"><button onclick="document.getElementById('result').textContent='Hello '+document.getElementById('name').value">Submit</button><p id="result" role="status">Waiting</p></body></html>'''

@dataclass
class ScriptedModelArgs:
    name: str = 'scripted-no-llm'
    def make_model(self):
        return ScriptedModel()

class ScriptedModel:
    response = None
    def __call__(self, messages):
        if self.response is None: raise RuntimeError('missing scripted response')
        return AIMessage('```\n'+self.response+'\n```')

def bid(obs, role, name):
    nodes=[n for n in obs['axtree_object']['nodes'] if n.get('role',{}).get('value')==role and n.get('name',{}).get('value')==name and n.get('browsergym_id')]
    if len(nodes)!=1: raise RuntimeError('element not unique')
    return nodes[0]['browsergym_id']

def main():
    env=gym.make('browsergym/openended',task_kwargs={'start_url':'data:text/html,'+quote(HTML)},headless=True,pre_observation_delay=0.1)
    agent=MostBasicAgent(temperature=0,use_chain_of_thought=False,chat_model_args=ScriptedModelArgs())
    try:
        obs,_=env.reset(seed=42)
        actions=[]
        for role,name,verb in [('textbox','Name','fill'),('button','Submit','click')]:
            ident=bid(obs,role,name)
            expected=f"fill({ident!r}, 'Ada')" if verb=='fill' else f'click({ident!r})'
            agent.chat.response=expected
            agent_obs=dict(obs)
            agent_obs['pruned_html']=prune_html(flatten_dom_to_str(obs['dom_object'],extra_properties=obs['extra_element_properties']))
            action,info=agent.get_action(agent_obs)
            if action.strip()!=expected: raise RuntimeError('AgentLab returned unexpected action')
            obs,_,_,_,_=env.step(action)
            if obs['last_action_error']: raise RuntimeError(obs['last_action_error'])
            actions.append(action)
        text=[n.get('name',{}).get('value','') for n in obs['axtree_object']['nodes']]
        if 'Hello Ada' not in text: raise RuntimeError('browser state did not update')
        print(json.dumps({'ok':True,'agent':'AgentLab MostBasicAgent','model':'scripted response, no LLM','actions':actions,'result':'Hello Ada'}))
    finally: env.close()

if __name__=='__main__': main()
