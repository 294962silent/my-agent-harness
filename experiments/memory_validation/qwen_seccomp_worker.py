"""Study-only 4096-token CUDA lane; full session prefetch, no truncation.

Uses the frozen Qwen pooling/model verification helpers and existing OS seccomp.
Old 1024-token production and archived experiment sources remain unchanged.
"""

import argparse
import json
import math
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root / "src"), str(root)]
from experiments.longmemeval.harness_retrieval import deny_network
from my_agent_core._memory_qwen_worker import (
    MODEL_REPO,
    MODEL_REVISION,
    DIMENSIONS,
    WorkerInitError,
    verify_model_files,
    query_text,
    last_token_pool,
)


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--device", required=True, choices=["cuda:0"])
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--max-length", type=int, required=True)
    parser.add_argument("--ownership-handshake", action="store_true")
    args = parser.parse_args()
    deny_network()
    if args.ownership_handshake:
        start_ticks = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
        print(json.dumps({"owned": True, "pid": os.getpid(), "start_ticks": start_ticks}), flush=True)
    if not 1 <= args.batch_size <= 4 or not 64 <= args.max_length <= 4096:
        raise ValueError("invalid bounded batch/token length")
    root = verify_model_files(args.model_dir)
    import torch
    import torch.nn.functional as functional
    from transformers import AutoModel, AutoTokenizer

    if not torch.cuda.is_available():
        raise WorkerInitError("cuda_unavailable")
    capability = torch.cuda.get_device_capability(0)
    if capability[0] >= 12 and f"sm_{capability[0]}{capability[1]}" not in torch.cuda.get_arch_list():
        raise WorkerInitError("cuda_architecture_unsupported")
    tokenizer = AutoTokenizer.from_pretrained(
        str(root), local_files_only=True, trust_remote_code=False, token=False, padding_side="left"
    )
    model = (
        AutoModel.from_pretrained(
            str(root),
            local_files_only=True,
            trust_remote_code=False,
            token=False,
            use_safetensors=True,
            dtype=torch.float16,
            attn_implementation="sdpa",
        )
        .to(args.device)
        .eval()
    )
    if model.config.hidden_size != DIMENSIONS:
        raise ValueError("official embedding dimensions differ")
    metadata = {
        "model": MODEL_REPO,
        "model_revision": MODEL_REVISION,
        "dimensions": DIMENSIONS,
        "device": str(next(model.parameters()).device),
        "device_name": torch.cuda.get_device_name(0),
        "compute_capability": list(capability),
        "torch_version": torch.__version__,
        "pid": os.getpid(),
        "cuda_device_total_bytes": torch.cuda.get_device_properties(0).total_memory,
        "max_length": args.max_length,
        "batch_size": args.batch_size,
        "normalized": True,
        "network_guard": "linux_seccomp_no_non_unix_socket_or_connect",
    }
    print(json.dumps({"ready": True, **metadata}), flush=True)
    while True:
        line = sys.stdin.readline(1024 * 1024 + 1)
        if not line:
            break
        identifier = None
        try:
            if len(line.encode("utf-8")) > 1024 * 1024 or not line.endswith("\n"):
                raise ValueError("request size exceeded")
            request = json.loads(line)
            identifier = request.get("id")
            if request.get("op") != "embed" or request.get("action") not in ("search", "document"):
                raise ValueError("unsupported operation")
            texts = request["texts"]
            if (
                not isinstance(texts, list)
                or not 1 <= len(texts) <= args.batch_size
                or any(not isinstance(t, str) or not t or len(t) > 65536 for t in texts)
            ):
                raise ValueError("invalid bounded batch")
            texts = [query_text(text, request["action"]) for text in texts]
            lengths = [len(x) for x in tokenizer(texts, truncation=False, padding=False)["input_ids"]]
            if any(length > args.max_length for length in lengths):
                raise ValueError("token_limit_exceeded")
            inputs = tokenizer(texts, truncation=False, padding=True, return_tensors="pt").to(args.device)
            with torch.inference_mode():
                result = model(**inputs)
                vectors = functional.normalize(
                    last_token_pool(result.last_hidden_state, inputs["attention_mask"]).float(), p=2, dim=1
                )
            rows = vectors.cpu().tolist()
            if any(len(v) != DIMENSIONS or any(not math.isfinite(x) for x in v) for v in rows):
                raise ValueError("invalid embedding vector")
            print(
                json.dumps(
                    {
                        "id": identifier,
                        "vectors": rows,
                        "metadata": {
                            "token_lengths": lengths,
                            "cuda_allocated_bytes": torch.cuda.memory_allocated(0),
                            "cuda_reserved_bytes": torch.cuda.memory_reserved(0),
                            "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
                            "cuda_peak_reserved_bytes": torch.cuda.max_memory_reserved(0),
                        },
                    }
                ),
                flush=True,
            )
        except Exception as error:
            # No facts, full exceptions or credentials cross the error channel.
            code = (
                "token_limit_exceeded"
                if isinstance(error, ValueError) and str(error) == "token_limit_exceeded"
                else type(error).__name__
            )
            print(json.dumps({"id": identifier, "error": code}), flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(json.dumps({"ready": False, "error": getattr(error, "code", type(error).__name__)}), flush=True)
        raise SystemExit(1)
