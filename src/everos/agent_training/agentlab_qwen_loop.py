"""Safe action checks and the real AgentLab + BrowserGym Qwen integration smoke."""

import argparse
import ast
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit


def _field(node, key):
    value = node.get(key, {}) if isinstance(node, dict) else {}
    return value.get("value") if isinstance(value, dict) else None


def visible_bids(obs):
    tree = obs.get("axtree_object", {}) if isinstance(obs, dict) else {}
    nodes = tree.get("nodes", []) if isinstance(tree, dict) else []
    bids = set()
    for node in nodes:
        bid = node.get("browsergym_id") if isinstance(node, dict) else None
        if isinstance(bid, str) and bid:
            bids.add(bid)
    return bids


def unique_button_bid(obs, button_name):
    tree = obs.get("axtree_object", {}) if isinstance(obs, dict) else {}
    nodes = tree.get("nodes", []) if isinstance(tree, dict) else []
    matches = [
        node.get("browsergym_id")
        for node in nodes
        if isinstance(node, dict)
        and _field(node, "role") == "button"
        and _field(node, "name") == button_name
        and isinstance(node.get("browsergym_id"), str)
        and node["browsergym_id"]
    ]
    if len(matches) != 1:
        raise ValueError(f"button {button_name!r} is not uniquely visible")
    return matches[0]


def confirm_bid_from_observation(obs):
    return unique_button_bid(obs, "Confirm")


def validate_browser_action(source, observed_bids):
    try:
        parsed = ast.parse(source.strip(), mode="eval").body
    except (SyntaxError, ValueError) as exc:
        raise ValueError("invalid BrowserGym action syntax") from exc
    if (
        not isinstance(parsed, ast.Call)
        or not isinstance(parsed.func, ast.Name)
        or parsed.func.id not in {"click", "fill", "select_option"}
        or parsed.keywords
    ):
        raise ValueError("unsupported BrowserGym action")
    if not parsed.args or not isinstance(parsed.args[0], ast.Constant):
        raise ValueError("action must start with a literal observed bid")
    bid = parsed.args[0].value
    if not isinstance(bid, str) or bid not in set(observed_bids):
        raise ValueError("action bid is not visible in the current observation")
    if parsed.func.id == "click" and len(parsed.args) != 1:
        raise ValueError("click accepts exactly one argument in this smoke")
    if parsed.func.id == "fill":
        if len(parsed.args) != 2 or not isinstance(parsed.args[1], ast.Constant) or not isinstance(parsed.args[1].value, str):
            raise ValueError("fill requires a literal string value")
    if parsed.func.id == "select_option" and len(parsed.args) != 2:
        raise ValueError("select_option requires a bid and option value")
    return ast.unparse(parsed)


def is_expected_call(source, function_name, argument):
    try:
        parsed = ast.parse(source.strip(), mode="eval").body
    except (SyntaxError, ValueError):
        return False
    return (
        isinstance(parsed, ast.Call)
        and isinstance(parsed.func, ast.Name)
        and parsed.func.id == function_name
        and len(parsed.args) == 1
        and not parsed.keywords
        and isinstance(parsed.args[0], ast.Constant)
        and parsed.args[0].value == argument
    )


def parse_agentlab_sft_action(response, observed_bids):
    """Parse the published ADP JSON action without evaluating model output."""
    from .agentlab_action import parse_agent_action

    try:
        return parse_agent_action(response, observed_bids)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise ValueError(f"invalid ADP JSON action response: {exc}") from exc


def build_sft_user_prompt(goal, action_space, accessibility_tree):
    return (
        f"# Goal\n{goal}\n\n"
        f"# Action Space\n{action_space}\n\n"
        f"# Current page Accessibility Tree\n{accessibility_tree}\n\n"
        'Return one JSON object with "thought" and "action" fields. '
        "The action field must contain exactly one supported BrowserGym action call."
    )


def validate_local_api_base(base_url):
    parts = urlsplit(base_url)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.port is None
        or parts.path.rstrip("/") != "/v1"
        or parts.query
        or parts.fragment
    ):
        raise ValueError("OPENAI_BASE_URL must be a local http://127.0.0.1:<port>/v1 URL")


def ensure_loopback_no_proxy():
    """Keep AgentLab's OpenAI SDK off inherited proxies for our local API endpoint."""
    for key in ("NO_PROXY", "no_proxy"):
        entries = [part.strip() for part in os.environ.get(key, "").split(",") if part.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in entries:
                entries.append(host)
        os.environ[key] = ",".join(entries)


def run_loop(model_name, result_dir, max_steps=2):
    """Run live model -> AgentLab SFT JSON parser -> BrowserGym -> new observation."""
    base_url = os.environ["OPENAI_BASE_URL"]
    validate_local_api_base(base_url)
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required (a local placeholder is accepted)")
    if not model_name:
        raise ValueError("model name is required")
    if max_steps != 2:
        raise ValueError("the integration smoke has exactly two expected actions")
    ensure_loopback_no_proxy()

    import browsergym.core  # noqa: F401 - registers browsergym/openended
    import bgym
    import gymnasium as gym
    from browsergym.utils.obs import flatten_axtree_to_str, flatten_dom_to_str, prune_html
    from agentlab.llm.chat_api import OpenAIModelArgs
    from agentlab.llm.llm_utils import Discussion, HumanMessage, SystemMessage

    output_dir = Path(result_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "agentlab-qwen-loop-result.json"
    if result_path.exists():
        raise FileExistsError(f"result exists; use a new directory: {result_path}")

    html = """<!doctype html><html><body>
    <button id="confirm" onclick="document.getElementById('confirm').remove();
      const b=document.createElement('button'); b.textContent='Continue';
      b.onclick=()=>document.getElementById('status').textContent='Task complete';
      document.body.insertBefore(b, document.getElementById('status'))">Confirm</button>
    <p id="status" role="status">Waiting</p></body></html>"""
    goal = (
        "Click the Confirm button. After the page changes and shows a Continue button, "
        "click Continue. Use one action at a time."
    )
    system = (
        "# Instructions\n"
        "You are a UI Assistant. Review the user's goal and current browser observation, "
        "then provide the best next action. The answer is consumed by a program. "
        'Return a JSON object with "thought" and "action" fields; action must be one call '
        "from the listed action space and use a bid present in the current observation."
    )
    result = {
        "ok": False,
        "evaluation_type": "agentlab_browsergym_integration_smoke",
        "action_protocol": "adp_json_action",
        "model_name": model_name,
        "openai_base_url": base_url,
        "model_revision": os.environ.get("AL_MODEL_REVISION", "unknown"),
        "model_fingerprint": os.environ.get("AL_MODEL_FINGERPRINT", "not-recorded"),
        "steps": [],
    }
    env = None
    try:
        model_args = OpenAIModelArgs(
            model_name=model_name,
            temperature=0,
            max_new_tokens=256,
        )
        class AgentLabSFTJsonAgent(bgym.Agent):
            def __init__(self):
                self.chat = model_args.make_model()
                self.action_set = bgym.HighLevelActionSet(["bid"], multiaction=False)
                self.last_response = None

            def get_action(self, current_obs):
                tree = flatten_axtree_to_str(
                    current_obs["axtree_object"],
                    extra_properties=current_obs["extra_element_properties"],
                )
                prompt = build_sft_user_prompt(
                    goal,
                    self.action_set.describe(with_long_description=False),
                    tree,
                )
                messages = Discussion(SystemMessage(system))
                messages.append(HumanMessage(prompt))
                answer = self.chat(messages)
                self.last_response = answer.get("content", "")
                action = parse_agentlab_sft_action(self.last_response, visible_bids(current_obs))
                action = validate_browser_action(action, visible_bids(current_obs))
                return action, bgym.AgentInfo(
                    think=self.last_response,
                    chat_messages=messages,
                    stats={"prompt_length": len(prompt), "response_length": len(self.last_response)},
                    extra_info={"action_protocol": "adp_json_action", "chat_model_args": model_args.__dict__},
                )

        agent = AgentLabSFTJsonAgent()
        env = gym.make(
            "browsergym/openended",
            task_kwargs={"start_url": "data:text/html," + quote(html)},
            headless=True,
            pre_observation_delay=0.1,
        )
        obs, _ = env.reset(seed=42)
        for step_number, expected_name in enumerate(("Confirm", "Continue"), start=1):
            expected_bid = unique_button_bid(obs, expected_name)
            observation_before = flatten_axtree_to_str(
                obs["axtree_object"], extra_properties=obs.get("extra_element_properties")
            )
            try:
                action, info = agent.get_action(obs)
            except Exception as exc:
                result["steps"].append(
                    {
                        "step": step_number,
                        "observed_button": expected_name,
                        "observed_bid": expected_bid,
                        "model_response": agent.last_response,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                raise
            checked_action = validate_browser_action(action, visible_bids(obs))
            if not is_expected_call(checked_action, "click", expected_bid):
                raise ValueError(
                    f"model must click the visible {expected_name!r} button "
                    f"with bid {expected_bid!r}; produced {checked_action!r}"
                )
            obs, _, _, _, _ = env.step(checked_action)
            if obs.get("last_action_error"):
                raise RuntimeError(f"BrowserGym action failed: {obs['last_action_error']}")
            observation_after = flatten_axtree_to_str(
                obs["axtree_object"], extra_properties=obs.get("extra_element_properties")
            )
            next_feedback = (
                {
                    "visible_button": "Continue",
                    "visible_bid": unique_button_bid(obs, "Continue"),
                }
                if step_number == 1
                else {"page_contains_completion": "Task complete" in observation_after}
            )
            result["steps"].append(
                {
                    "step": step_number,
                    "observed_button": expected_name,
                    "observed_bid": expected_bid,
                    "action": checked_action,
                    "model_response": info.get("think"),
                    "browser_feedback_after_action": next_feedback,
                    "page_changed": observation_before != observation_after,
                }
            )
        final_page = prune_html(
            flatten_dom_to_str(
                obs["dom_object"], extra_properties=obs["extra_element_properties"]
            )
        )
        result["final_observation"] = final_page
        if "Task complete" not in final_page:
            raise ValueError("expected completion feedback was not present in the page")
        result["ok"] = True
    except Exception as exc:  # Save model and browser failures as experiment outcomes.
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if env is not None:
            env.close()
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name", default=os.environ.get("AL_MODEL_NAME"))
    parser.add_argument("--result-dir", default=os.environ.get("AL_LOOP_DIR"))
    parser.add_argument("--max-steps", type=int, default=2)
    args = parser.parse_args()
    if not args.model_name or not args.result_dir:
        parser.error("set AL_MODEL_NAME and AL_LOOP_DIR or pass both options")
    result = run_loop(args.model_name, args.result_dir, args.max_steps)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
