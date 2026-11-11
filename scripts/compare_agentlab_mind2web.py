#!/usr/bin/env python3
"""Verify and summarize paired base/adapter Mind2Web evaluation reports."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from everos.agent_training.agentlab_mind2web_eval import compare_pair_reports

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-report", type=Path, required=True)
    parser.add_argument("--adapter-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"comparison output already exists: {args.output}")
    base = json.loads(args.base_report.read_text(encoding="utf-8"))
    adapter = json.loads(args.adapter_report.read_text(encoding="utf-8"))
    comparison = compare_pair_reports(base, adapter)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(comparison, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
