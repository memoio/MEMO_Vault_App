# Embedded local reranker

EverOS supports `provider = "local"` for a single-label CrossEncoder such as
`BAAI/bge-reranker-base`. It runs inside the core API process; no inference
server or port is needed. Each API worker owns its own model copy. Prefer one
worker on memory-constrained machines.

## Installation

Install dependencies in the same environment that runs EverOS:

```bash
cd /root/EverOS
.venv/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install 'sentence-transformers>=6.2,<7'
```

The project also declares the `local-rerank` optional dependency group. Keep it
selected when synchronizing the environment. Install the CPU torch wheel first
when a GPU build is unnecessary.

Download the model once with Hugging Face CLI. Repeat `--include` for each
pattern (passing multiple patterns after a single flag makes some CLI versions
treat the remaining patterns as literal filenames):

```bash
hf download BAAI/bge-reranker-base \
  --local-dir /root/models/bge-reranker-base \
  --include '*.json' \
  --include '*.safetensors' \
  --include '*.model' \
  --include 'vocab.txt'
```

The directory must include weights, `config.json`, and tokenizer files. The
runtime only reads local files, uses safetensors, and disables remote code.
It never downloads missing files on a request.

## Configuration

Replace the existing `[rerank]` section in the configuration used by the core
API (`/root/EverOS/.everos-ollama/everos.toml` in this workspace):

```toml
[rerank]
provider = "local"
model = "/root/models/bge-reranker-base"
device = "cpu"
max_length = 512
batch_size = 4
max_concurrent = 1
timeout_seconds = 120
```

No API key or base URL is required. Other HTTP providers retain their existing
configuration. `EVEROS_RERANK__DEVICE` and `EVEROS_RERANK__MAX_LENGTH`, like other
rerank environment variables, override TOML values. BGE base truncates pairs to
512 tokens; choose suitably sized Knowledge chunks.

Restart the core EverOS API using its normal startup command and the same data
home. No separate reranker startup is necessary. The first rerank request loads
the model; later requests reuse it. Capability availability checks validate
configuration, files and dependency presence, but do not warm up the model.
Actual loading/inference failures surface as `RerankServiceError`.

Inference runs in worker threads. A thread semaphore limits actual concurrent
work, including native work that outlives a cancelled or timed-out request.
Timeout covers queueing, loading and scoring; native inference cannot be killed
mid-batch. Expired workers stop before their next batch. Scores use sigmoid
activation and return all input indices in descending relevance order.

After restarting, the previously rejected Claude Knowledge document deliveries
can be retried through the existing Vault delivery action.
