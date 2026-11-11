"""Paired, offline next-action imitation evaluation for held-out Mind2Web data."""

import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


MAX_COMPLETION_TOKENS = 256
TEMPERATURE = 0
REVISION = "2fc06364715b967f1860aea9cf38778875588b17"
DIRECT_OPENER = build_opener(ProxyHandler({}))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strip_fence(text):
    if not isinstance(text, str):
        raise TypeError("model response must be text")
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
        if value.startswith("json"):
            value = value[4:].strip()
    return value


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, int, float, bool, type(None))
    ):
        if isinstance(node.value, float) and not math.isfinite(node.value):
            raise ValueError("non-finite action argument")
        return node.value
    if isinstance(node, ast.List):
        return [_literal(value) for value in node.elts]
    if isinstance(node, ast.Tuple):
        return {"__tuple__": [_literal(value) for value in node.elts]}
    if isinstance(node, ast.Dict):
        return {str(_literal(key)): _literal(value) for key, value in zip(node.keys, node.values, strict=True)}
    raise ValueError("action arguments must be Python literals")


def normalize_action_expression(source):
    """Return a stable JSON call signature; never executes parsed source."""
    try:
        node = ast.parse(source.strip(), mode="eval").body
    except (SyntaxError, ValueError, TypeError):
        return None
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or not node.func.id.isidentifier()
        or node.func.id.startswith("_")
        or any(isinstance(arg, ast.Starred) for arg in node.args)
        or any(keyword.arg is None for keyword in node.keywords)
    ):
        return None
    values = {}
    try:
        for index, arg in enumerate(node.args):
            values[f"arg{index}"] = _literal(arg)
        for keyword in node.keywords:
            if keyword.arg in values:
                return None
            values[keyword.arg] = _literal(keyword.value)
    except (ValueError, TypeError, RecursionError):
        return None
    known_positional = {
        "click": ("bid", "button", "modifiers"),
        "fill": ("bid", "value"),
        "select_option": ("bid", "options"),
        "goto": ("url",),
        "send_msg_to_user": ("text",),
        "noop": ("wait_ms",),
        "scroll": ("delta_x", "delta_y"),
        "type": ("value", "bid"),
        "select": ("value", "bid"),
    }.get(node.func.id, ())
    normalized = {}
    for index, value in enumerate(values.pop(f"arg{i}", None) for i in range(len(node.args))):
        name = known_positional[index] if index < len(known_positional) else f"arg{index}"
        normalized[name] = value
    for name, value in values.items():
        if name.startswith("arg"):
            normalized[name] = value
        elif name in normalized:
            return None
        else:
            normalized[name] = value
    return json.dumps(
        {"function": node.func.id, "arguments": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_target_action(text):
    """Extract ``(action name, canonical AST signature)`` from an ADP target."""
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(_strip_fence(text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
        return None
    normalized = normalize_action_expression(payload["action"])
    if normalized is None:
        return None
    try:
        action = json.loads(normalized)["function"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return action, normalized


def select_test_manifest_rows(rows):
    test_rows = [row for row in rows if row.get("split") == "test"]
    if not test_rows:
        raise ValueError("manifest has no held-out test rows")
    if any(row.get("split") not in {"train", "validation", "test"} for row in rows):
        raise ValueError("manifest contains an unknown split")
    ids = [row.get("id") for row in test_rows]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("test manifest contains missing or duplicate IDs")
    return test_rows


def evaluate_records(targets, predictions):
    target_ids = [item.get("id") for item in targets]
    prediction_ids = [item.get("id") for item in predictions]
    if target_ids != prediction_ids:
        raise ValueError("prediction IDs/order do not match held-out targets")
    if not targets:
        raise ValueError("no test decisions to evaluate")
    n = len(targets)
    target_parsed = [parse_target_action(item.get("target", "")) for item in targets]
    prediction_parsed = [parse_target_action(item.get("response_text", "")) for item in predictions]
    valid_targets = [i for i, action in enumerate(target_parsed) if action is not None]
    scored = [i for i in valid_targets if prediction_parsed[i] is not None]
    type_matches = sum(target_parsed[i][0] == prediction_parsed[i][0] for i in scored)
    exact_matches = sum(target_parsed[i][1] == prediction_parsed[i][1] for i in scored)
    trajectory_ids = [item.get("trajectory_id") or item["id"].rsplit("-", 1)[0] for item in targets]
    grouped = {}
    for index, trajectory_id in enumerate(trajectory_ids):
        grouped.setdefault(trajectory_id, []).append(index)
    eligible_groups = [
        indexes
        for indexes in grouped.values()
        if all(target_parsed[index] is not None for index in indexes)
    ]
    exact_group_count = sum(
        all(
            prediction_parsed[index] is not None
            and prediction_parsed[index][1] == target_parsed[index][1]
            for index in indexes
        )
        for indexes in eligible_groups
    )
    valid_json = sum(_valid_target_json(item.get("response_text", "")) for item in predictions)
    invalid_action = sum(
        _valid_target_json(item.get("response_text", "")) and prediction_parsed[index] is None
        for index, item in enumerate(predictions)
    )
    return {
        "decision_count": n,
        "scored_decision_count": len(scored),
        "valid_target_count": len(valid_targets),
        "invalid_target_count": n - len(valid_targets),
        "valid_json_count": valid_json,
        "valid_json_rate": valid_json / n,
        "invalid_json_rate": (n - valid_json) / n,
        "action_parse_count": sum(action is not None for action in prediction_parsed),
        "action_parse_rate": sum(action is not None for action in prediction_parsed) / n,
        "invalid_action_count": invalid_action,
        "invalid_action_rate": invalid_action / n,
        "action_type_accuracy": type_matches / len(scored) if scored else 0.0,
        "action_exact_match": exact_matches / len(valid_targets) if valid_targets else 0.0,
        "trajectory_count": len(grouped),
        "scored_trajectory_count": len(eligible_groups),
        "trajectory_all_actions_match": (
            exact_group_count / len(eligible_groups) if eligible_groups else 0.0
        ),
    }


def _valid_target_json(text):
    try:
        payload = json.loads(_strip_fence(text))
        return isinstance(payload, dict) and isinstance(payload.get("action"), str)
    except (json.JSONDecodeError, TypeError):
        return False


def compare_pair_reports(base_report, adapter_report):
    if base_report.get("model_tag") != "base" or adapter_report.get("model_tag") != "adapter":
        raise ValueError("comparison requires one base report and one adapter report")
    if base_report.get("test_manifest_sha256") != adapter_report.get("test_manifest_sha256"):
        raise ValueError("base and adapter runs used different test manifests")
    if base_report.get("test_data_sha256") != adapter_report.get("test_data_sha256"):
        raise ValueError("base and adapter runs used different held-out test data")
    if base_report.get("ordered_test_ids") != adapter_report.get("ordered_test_ids"):
        raise ValueError("base and adapter runs have different test IDs or ordering")
    if base_report.get("generation") != adapter_report.get("generation"):
        raise ValueError("base and adapter runs used different generation settings")
    if base_report.get("model_name") != adapter_report.get("model_name"):
        raise ValueError("base and adapter runs report different Qwen model IDs")
    if base_report.get("model_revision") != adapter_report.get("model_revision"):
        raise ValueError("base and adapter runs use different Qwen revisions")
    if base_report.get("status") != "complete" or adapter_report.get("status") != "complete":
        raise ValueError("both evaluation runs must complete before comparison")
    return {
        "evaluation_type": "offline_next_action_imitation",
        "same_test_manifest": True,
        "test_data_sha256": base_report["test_data_sha256"],
        "test_manifest_sha256": base_report["test_manifest_sha256"],
        "decision_count": len(base_report["ordered_test_ids"]),
        "generation": base_report["generation"],
        "base": {
            "model_name": base_report["model_name"],
            "model_revision": base_report["model_revision"],
            "model_fingerprint": base_report["model_fingerprint"],
            "metrics": base_report["metrics"],
        },
        "adapter": {
            "model_name": adapter_report["model_name"],
            "model_revision": adapter_report["model_revision"],
            "model_fingerprint": adapter_report["model_fingerprint"],
            "metrics": adapter_report["metrics"],
        },
        "delta_adapter_minus_base": {
            key: adapter_report["metrics"][key] - base_report["metrics"][key]
            for key in (
                "valid_json_rate",
                "action_parse_rate",
                "action_type_accuracy",
                "action_exact_match",
                "trajectory_all_actions_match",
            )
        },
        "caveat": (
            "Decision-level imitation against published SFT targets; this is not live website "
            "task success and does not recover unknown original loss masks."
        ),
    }


def load_test_data(data_dir):
    data_dir = Path(data_dir)
    report = json.loads((data_dir / "report.json").read_text(encoding="utf-8"))
    manifest_path = data_dir / "manifest.jsonl"
    data_path = data_dir / "test.jsonl"
    manifest_rows = [json.loads(line) for line in manifest_path.open(encoding="utf-8")]
    test_rows = select_test_manifest_rows(manifest_rows)
    if sha256_file(manifest_path) != report["output_sha256"]["manifest.jsonl"]:
        raise ValueError("manifest hash differs from prepared-data report")
    if sha256_file(data_path) != report["output_sha256"]["test.jsonl"]:
        raise ValueError("test data hash differs from prepared-data report")
    decisions = [json.loads(line) for line in data_path.open(encoding="utf-8")]
    if len(decisions) != len(test_rows):
        raise ValueError("test data count differs from the test manifest")
    examples = []
    for decision, meta in zip(decisions, test_rows, strict=True):
        messages = decision.get("conversations", [])
        if (
            not isinstance(decision.get("system"), str)
            or len(messages) != 2
            or messages[0].get("from") != "human"
            or messages[1].get("from") != "gpt"
        ):
            raise ValueError(f"invalid test prompt/target record: {meta['id']}")
        examples.append(
            {
                "id": meta["id"],
                "trajectory_id": meta.get("trajectory_id"),
                "system": decision["system"],
                "user": messages[0]["value"],
                "target": messages[1]["value"],
            }
        )
    return examples, sha256_file(data_path), sha256_file(manifest_path)


def _chat_completion(base_url, api_key, model_name, example, timeout):
    parts = urlsplit(base_url)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.port is None
        or parts.path.rstrip("/") != "/v1"
        or parts.query
        or parts.fragment
    ):
        raise ValueError("evaluation API URL must be a loopback http://127.0.0.1:<port>/v1")
    body = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": example["system"]},
            {"role": "user", "content": example["user"]},
        ],
        "temperature": TEMPERATURE,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "stream": False,
    }
    request = Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with DIRECT_OPENER.open(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        raise RuntimeError(f"chat completion returned HTTP {exc.code}: {exc.read()[:500]!r}") from exc
    except URLError as exc:
        raise RuntimeError(f"chat completion request failed: {exc}") from exc
    content = payload["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("chat completion returned no text content")
    return content, payload.get("usage")


def run_evaluation(data_dir, model_tag, api_url, api_key, model_name, model_fingerprint, output_dir, timeout=300):
    if model_tag not in {"base", "adapter"}:
        raise ValueError("model-tag must be base or adapter")
    if not model_fingerprint or not model_fingerprint.strip():
        raise ValueError("a model fingerprint is required")
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    examples, test_sha, manifest_sha = load_test_data(data_dir)
    output.mkdir(parents=True, exist_ok=False)
    predictions = []
    started = time.monotonic()
    predictions_path = output / "predictions.jsonl"
    try:
        with predictions_path.open("x", encoding="utf-8") as stream:
            for example in examples:
                response_text, usage = _chat_completion(api_url, api_key, model_name, example, timeout)
                prediction = {
                    "id": example["id"],
                    "trajectory_id": example["trajectory_id"],
                    "response_text": response_text,
                    "usage": usage,
                }
                predictions.append(prediction)
                stream.write(json.dumps(prediction, ensure_ascii=False) + "\n")
                stream.flush()
    except Exception as exc:
        failure_report = {
            "evaluation_type": "offline_next_action_imitation",
            "status": "failed",
            "model_tag": model_tag,
            "model_name": model_name,
            "model_fingerprint": model_fingerprint,
            "model_revision": REVISION,
            "api_url": api_url,
            "generation": {"temperature": TEMPERATURE, "max_completion_tokens": MAX_COMPLETION_TOKENS},
            "test_data_sha256": test_sha,
            "test_manifest_sha256": manifest_sha,
            "ordered_test_ids": [item["id"] for item in examples],
            "completed_prediction_count": len(predictions),
            "predictions_sha256": sha256_file(predictions_path),
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        (output / "report.json").write_text(
            json.dumps(failure_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        raise
    metrics = evaluate_records(examples, predictions)
    report = {
        "evaluation_type": "offline_next_action_imitation",
        "status": "complete",
        "model_tag": model_tag,
        "model_name": model_name,
        "model_fingerprint": model_fingerprint,
        "model_revision": REVISION,
        "api_url": api_url,
        "generation": {"temperature": TEMPERATURE, "max_completion_tokens": MAX_COMPLETION_TOKENS},
        "test_data_sha256": test_sha,
        "test_manifest_sha256": manifest_sha,
        "ordered_test_ids": [item["id"] for item in examples],
        "metrics": metrics,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "predictions_sha256": sha256_file(predictions_path),
        "caveat": (
            "Decision-level imitation against published SFT targets; not live website task "
            "success; original loss masks remain unknown."
        ),
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model-tag", choices=("base", "adapter"), required=True)
    parser.add_argument("--api-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:18082/v1"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", "local"))
    parser.add_argument("--model-name", default=os.environ.get("AL_MODEL_NAME", "qwen35"))
    parser.add_argument("--model-fingerprint", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    report = run_evaluation(
        args.data_dir,
        args.model_tag,
        args.api_url,
        args.api_key,
        args.model_name,
        args.model_fingerprint,
        args.output_dir,
        args.timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
