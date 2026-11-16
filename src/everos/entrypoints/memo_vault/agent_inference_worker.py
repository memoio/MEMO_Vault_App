"""Private JSON-lines worker; only the parent process can access stdin/stdout."""

import contextlib
import json
import os
import sys
from pathlib import Path


def main():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForImageTextToText, AutoTokenizer

    torch.set_num_threads(max(1, int(os.environ.get("MEMO_AGENT_CPU_THREADS", "4"))))
    root = Path(
        os.environ.get(
            "MEMO_AGENT_WEIGHTS",
            str(
                Path(__file__).resolve().parents[4]
                / ".memo-vault/model-weights-20261113"
            ),
        )
    )
    base = root / "qwen35-08b"
    requested_device = os.environ.get("MEMO_AGENT_DEVICE", "auto")
    device = (
        "cuda" if requested_device != "cpu" and torch.cuda.is_available() else "cpu"
    )
    model = None
    if device == "cuda":
        try:
            from transformers import BitsAndBytesConfig

            quantization = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
            model = AutoModelForImageTextToText.from_pretrained(
                str(base),
                quantization_config=quantization,
                device_map={"": "cuda:0"},
                dtype=torch.bfloat16,
                local_files_only=True,
            )
        except (torch.cuda.OutOfMemoryError, ImportError) as exc:
            print(
                f"GPU load unavailable, using CPU: {exc}", file=sys.stderr, flush=True
            )
            torch.cuda.empty_cache()
            device = "cpu"
    if device == "cpu":
        # CUDA-only kernel wrappers must not run on CPU tensors.
        from transformers.models.qwen3_5 import modeling_qwen3_5 as qwen35

        for name in (
            "causal_conv1d_fn",
            "causal_conv1d_update",
            "torch_chunk_gated_delta_rule",
            "torch_recurrent_gated_delta_rule",
        ):
            function = getattr(qwen35, name)
            setattr(qwen35, name, getattr(function, "__wrapped__", function))
        model = AutoModelForImageTextToText.from_pretrained(
            str(base), dtype=torch.float32, device_map="cpu", local_files_only=True
        )
    tokenizer = AutoTokenizer.from_pretrained(str(base), local_files_only=True)
    model = PeftModel.from_pretrained(
        model, str(root / "openhands"), adapter_name="openhands", is_trainable=False
    )
    for name in ("sweagent", "agentlab"):
        model.load_adapter(str(root / name), adapter_name=name, is_trainable=False)
    model.eval()
    mapping = {"openhands": "openhands", "swe": "sweagent", "agentlab": "agentlab"}
    for line in sys.stdin:
        try:
            req = json.loads(line)
            agent = req["agent"]
            history = req["messages"]
            if agent not in ("default", *mapping):
                raise ValueError("unknown agent")
            truncated = False
            # Bound the encoded prompt, always retaining the most recent user message.
            while True:
                inputs = tokenizer.apply_chat_template(
                    history,
                    tokenize=True,
                    add_generation_prompt=True,
                    return_tensors="pt",
                    enable_thinking=False,
                    return_dict=True,
                )
                if inputs["input_ids"].shape[-1] <= 2048:
                    break
                if len(history) <= 1 or history[0]["role"] == "system":
                    raise ValueError("框架消息超过模型上下文预算，请缩短对话")
                history = history[2:] if len(history) > 2 else history[-1:]
                truncated = True
            inputs = inputs.to(device)
            if agent != "default":
                model.set_adapter(mapping[agent])
            ctx = (
                model.disable_adapter()
                if agent == "default"
                else contextlib.nullcontext()
            )
            with ctx, torch.inference_mode():
                result = model.generate(
                    **inputs,
                    max_new_tokens=int(
                        os.environ.get("MEMO_AGENT_MAX_NEW_TOKENS", "256")
                    ),
                    do_sample=False,
                    pad_token_id=tokenizer.pad_token_id,
                )
            text = tokenizer.decode(
                result[0, inputs["input_ids"].shape[-1] :], skip_special_tokens=True
            )
            if truncated:
                text = "（对话较长，已省略较早的上下文。）\n\n" + text
            print(
                json.dumps(
                    {"text": text, "agent": agent, "device": device}, ensure_ascii=False
                ),
                flush=True,
            )
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
