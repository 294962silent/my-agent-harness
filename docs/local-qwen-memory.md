# Local Qwen3 GPU memory candidates

`local_qwen_gpu` is an opt-in Mem0 candidate index. It loads the official
`Qwen/Qwen3-Embedding-0.6B` revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`. It does not replace or change the
existing `local_cpu_zh` profile. This is a local embedding integration, not a
measured memory-quality improvement.

The application interpreter needs the existing pinned Mem0/Qdrant extra. It does
not need Torch. A separate interpreter runs `_memory_qwen_worker.py` directly
with `-I -X utf8 -u`, avoiding package initialization and Windows locale-dependent
stdio decoding. Prepare that interpreter explicitly;
the runtime never installs dependencies or downloads model files. One tested
Windows environment uses Torch 2.9.1+cu128, Transformers 4.57.6, HuggingFace Hub
0.36.2 and Safetensors 0.7.0. A compatible CUDA driver/device is required.

For example, create a separate virtual environment and install its dependencies
with its own Python/pip, using the official Torch cu128 wheel index. Do not run
these installation commands against the application's default environment.

```powershell
py -3.13 -m venv C:\chosen\qwen-runtime
& C:\chosen\qwen-runtime\Scripts\python.exe -m pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cu128
& C:\chosen\qwen-runtime\Scripts\python.exe -m pip install transformers==4.57.6 huggingface-hub==0.36.2 safetensors==0.7.0
```

## Prepare fixed public model files

The ten filenames, expected byte lengths, official Git blob IDs and LFS payload
SHA256 values are public constants `MODEL_FILES` in `_memory_qwen_worker.py`.
Download those files from
`https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/<filename>`
into a new directory preserving `1_Pooling/config.json`. This requires no model
account credentials. For small Git files, the corresponding `/raw/<revision>/`
URL supplies the original bytes; use `/resolve/` for LFS weights/tokenizer.

Use a preparation process separate from application startup. A native curl
invocation can use `--disable --fail --location --proto =https --proto-redir
=https --output <new-file> <official-url>`; never disable TLS validation. An
existing complete, hash-verified cache need not be downloaded again. No mirror
claim replaces the official content identities.

Verify the prepared directory using the application interpreter:

```python
from my_agent_core._memory_qwen_worker import verify_model_files
verify_model_files(r"C:\chosen\qwen-model")
```

Verification checks all ten sizes/content identities and rejects symlinks and
junctions. Weights use Safetensors, `trust_remote_code=False` and
`local_files_only=True`. The worker verifies again before loading. No conversion
or save is performed on the model cache. No credential/auth file is read by this
adapter's preparation or inference code.

## Explicit production configuration

```python
from my_agent_core.memory_qwen import local_qwen_config
from my_agent_core.memory_mem0 import create_mem0_index

config = local_qwen_config(
    model_dir=r"C:\chosen\qwen-model",
    python_executable=r"C:\chosen\qwen-runtime\Scripts\python.exe",
    storage_dir=r"C:\chosen\owned-memory-cache",
    device="cuda:0",
    batch_size=1,
    max_length=1024,
    startup_timeout=120,
    request_timeout=30,
)
index = create_mem0_index(config, timeout=60)
# Inject into MemoryStore(..., memory_index=index), then:
# await index.aclose(timeout=60)
```

The constructor returns a plain strict profile dictionary, suitable for the
existing memory configuration path. Unknown profile fields are rejected.
Global/user configuration is not changed automatically. `timeout` governs the
existing Mem0 search operation, including indexing. GPU startup has its separate
deadline. The entire corpus may take longer than an individual embedding request;
choose an operation timeout explicitly instead of treating the default as a
performance guarantee.

CUDA device 0 is explicit. Batch sizes are 1–4, and the initial bounded token cap
is 64–1024, including the query instruction and special tokens. Oversized inputs
produce `token_limit_exceeded`, never silent truncation. This does not claim the
model's full advertised context fits an 8GB device. Missing CUDA, unsupported
sm_120 builds, model mismatch, worker timeout or invalid vectors fail explicitly;
there is no BGE or CPU substitution.

Documents use unprefixed text. Searches use the fixed English instruction
`Given a web search query, retrieve relevant passages that answer the query` in
the official `Instruct: ...\nQuery: ...` format. Last-token pooling and L2
normalization produce 1024-dimensional vectors; CPU-host response checks enforce
finite values and unit norm. The Qdrant collection and owned directory are new
1024-dimensional instances, never reused from the BGE cache.

Mem0 receives `infer=False`, a NeverLLM instance and no reranker. This profile is
explicitly dense-only: the owned Qdrant subclass creates no BM25 sparse slot or
encoder. A Qwen-owned Memory subclass reuses pinned SDK method code with isolated
function globals: lemmatization becomes identity and entity extraction becomes
empty results. This prevents optional spaCy/BM25 model construction/downloads.
The SDK version/commit and LF-normalized source hashes of ten relevant original
Memory/Qdrant methods are checked before GPU startup; unknown source fails closed.
No SDK module global, factory registry or unrelated/BGE instance is patched.
This compatibility shim changes optional hybrid/NLP semantics and must be recorded
when comparing with another profile; it is not full vanilla SDK behavior. The
adapter does not construct an OpenAI client. Worker
environment variables remove API/auth/proxy credentials, force HuggingFace offline
flags and disable telemetry. This trusted-worker boundary is not an OS sandbox.
The worker additionally denies Python AF_INET/AF_INET6 socket creation. Host
production smoke applies its own Internet-socket rejection while exercising real
CUDA vectors and real Mem0/MemoryStore, separately from fake-worker tests.

The protocol is one owned process, JSON frames capped at 1MiB and serialized
request IDs. Write, flush and response share one deadline; a ready worker that
stops reading cannot block the caller indefinitely. Shutdown has separate bounded
process/reader/sender cleanup waits, so the request deadline is not a guarantee
of instant OS process teardown. Timeout/protocol failure opens the client circuit and terminates its process
tree. Closing the owned Mem0 index closes SQLite, Qdrant and the embedding worker;
`process_exited` confirms the child ended. On Windows a venv redirector may have a
different launcher PID and CUDA-worker PID; both are reported, and a native handle
to the actual worker is checked during close, in addition to process-tree cleanup.
Device/capability, token lengths,
allocated/reserved/peak CUDA bytes and worker PID are available in index profile
metadata. Desktop/driver memory may remain after the process exits; do not equate
that with a live worker or claim zero GPU usage.

CPU fake-worker tests verify transport/cleanup contracts only. Real GPU smoke
uses a few synthetic facts and English/Chinese queries, with no paid generation;
it is separate from quality evaluation and the frozen LongMemEval results.
