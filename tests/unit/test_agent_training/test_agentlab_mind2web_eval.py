import json
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from everos.agent_training.agentlab_mind2web_eval import (
    compare_pair_reports,
    evaluate_records,
    parse_target_action,
    run_evaluation,
    select_test_manifest_rows,
)


def test_parses_json_actions_and_markdown_fences_without_execution():
    action = 'click("a12")'
    plain = json.dumps({"thought": "", "action": action})
    fenced = f"```json\n{plain}\n```"
    assert parse_target_action(plain) == parse_target_action(fenced)
    assert parse_target_action(plain)[0] == "click"
    assert parse_target_action(json.dumps({"action": "type('Ada', 'b2')"})) == parse_target_action(
        json.dumps({"action": "type(value='Ada', bid='b2')"})
    )
    assert parse_target_action(json.dumps({"action": "select('New York', 'b3')"})) == parse_target_action(
        json.dumps({"action": "select(value='New York', bid='b3')"})
    )


@pytest.mark.parametrize(
    "text",
    ["not json", "{}", '{"action": "not a call"}', '{"action": "x()\n__import__(\"os\")"}'],
)
def test_malformed_or_unsafe_action_is_not_scored_as_valid(text):
    assert parse_target_action(text) is None


def test_nonliteral_action_argument_is_rejected_without_execution():
    assert parse_target_action(json.dumps({"action": "fill('bid', value=not_a_literal)"})) is None


def test_canonical_action_metrics_and_trajectory_aggregation():
    targets = [
        {"id": "t1-0", "trajectory_id": "t1", "target": json.dumps({"action": "click('b1')"})},
        {"id": "t1-1", "trajectory_id": "t1", "target": json.dumps({"action": "fill('b2', 'Ada')"})},
        {"id": "t2-0", "trajectory_id": "t2", "target": json.dumps({"action": "goto(url='https://x.test')"})},
    ]
    predictions = [
        {"id": "t1-0", "response_text": json.dumps({"action": 'click("b1")'})},
        {"id": "t1-1", "response_text": json.dumps({"action": "fill(bid='b2', value='Ada')"})},
        {"id": "t2-0", "response_text": json.dumps({"action": "goto(url='https://wrong.test')"})},
    ]

    metrics = evaluate_records(targets, predictions)

    assert metrics["decision_count"] == 3
    assert metrics["action_parse_rate"] == 1
    assert metrics["action_type_accuracy"] == 1
    assert metrics["action_exact_match"] == pytest.approx(2 / 3)
    assert metrics["trajectory_all_actions_match"] == pytest.approx(1 / 2)


def test_manifest_is_test_only_and_pair_requires_same_ids_and_hash():
    rows = [
        {"id": "a", "split": "test", "trajectory_id": "t"},
        {"id": "b", "split": "train", "trajectory_id": "u"},
    ]
    assert select_test_manifest_rows(rows) == [rows[0]]
    with pytest.raises(ValueError):
        select_test_manifest_rows([{"id": "a", "split": "validation"}])

    report = {
        "model_tag": "base",
        "status": "complete",
        "model_name": "qwen35",
        "model_revision": "2fc06364715b967f1860aea9cf38778875588b17",
        "model_fingerprint": "base-fingerprint",
        "test_manifest_sha256": "same",
        "test_data_sha256": "same-data",
        "ordered_test_ids": ["a"],
        "generation": {"temperature": 0},
        "metrics": {
            "valid_json_rate": 1.0,
            "action_parse_rate": 1.0,
            "action_type_accuracy": 1.0,
            "action_exact_match": 1.0,
            "trajectory_all_actions_match": 1.0,
        },
    }
    adapter = {**report, "model_tag": "adapter", "model_fingerprint": "adapter-fingerprint"}
    compare_pair_reports(report, adapter)
    with pytest.raises(ValueError):
        compare_pair_reports(report, {**adapter, "ordered_test_ids": ["b"]})
    with pytest.raises(ValueError):
        compare_pair_reports(report, {**adapter, "test_manifest_sha256": "other"})
    with pytest.raises(ValueError):
        compare_pair_reports(report, {**adapter, "test_data_sha256": "other-data"})


def test_eval_runner_uses_identical_deterministic_requests_and_writes_paired_reports(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    decision = {
        "system": "system prompt",
        "conversations": [
            {"from": "human", "value": "current observation"},
            {"from": "gpt", "value": json.dumps({"thought": "", "action": "click('b1')"})},
        ],
    }
    test_path = data_dir / "test.jsonl"
    test_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    manifest_path = data_dir / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps({"id": "task-0", "split": "test", "trajectory_id": "task"}) + "\n",
        encoding="utf-8",
    )
    (data_dir / "report.json").write_text(
        json.dumps(
            {
                "output_sha256": {
                    "test.jsonl": hashlib.sha256(test_path.read_bytes()).hexdigest(),
                    "manifest.jsonl": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                }
            }
        ),
        encoding="utf-8",
    )
    requests = []

    class CompletionHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {"choices": [{"message": {"content": json.dumps({"action": "click(\"b1\")"})}}], "usage": {}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CompletionHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        base = run_evaluation(
            data_dir, "base", api_url, "local", "qwen35",
            "base-fingerprint", tmp_path / "base", timeout=5,
        )
        adapter = run_evaluation(
            data_dir, "adapter", api_url, "local", "qwen35",
            "adapter-fingerprint", tmp_path / "adapter", timeout=5,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0]["temperature"] == 0
    assert requests[0]["max_completion_tokens"] == 256
    assert base["model_fingerprint"] == "base-fingerprint"
    assert adapter["model_fingerprint"] == "adapter-fingerprint"
    pair = compare_pair_reports(base, adapter)
    assert pair["same_test_manifest"] is True
    assert pair["decision_count"] == 1
