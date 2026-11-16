"""Standalone offline CUDA embedder. Direct -I execution never imports the package."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys

MODEL_REPO = "Qwen/Qwen3-Embedding-0.6B"
MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
DIMENSIONS = 1024
QUERY_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
# Official revision Git blob identities; LFS payloads use their independent SHA256.
MODEL_FILES = {
    "1_Pooling/config.json": (313, "b6291baabbc39f9792d6759883a47d7d5bd0fbcf", "git"),
    "config.json": (727, "cef2749ee93607b8f9a58ec72f4f6bfaf874e71d", "git"),
    "config_sentence_transformers.json": (215, "76aef3ade63553ebb698fe3c2a3264040ed093f8", "git"),
    "generation_config.json": (117, "d46f1983345269c582611bbedb3ca0a13f8e5f7b", "git"),
    "merges.txt": (1671853, "31349551d90c7606f325fe0f11bbb8bd5fa0d7c7", "git"),
    "model.safetensors": (1191586416, "0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd", "sha256"),
    "modules.json": (349, "952a9b81c0bfd99800fabf352f69c7ccd46c5e43", "git"),
    "tokenizer.json": (11423705, "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a", "sha256"),
    "tokenizer_config.json": (9706, "7345216a0785dc7086e8c245b2a9d3896ce2b756", "git"),
    "vocab.json": (2776833, "4783fe10ac3adce15ac8f358ef5462739852c569", "git"),
}


class WorkerInitError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def deny_internet_sockets():
    """Trusted-worker Python guard, explicitly not an OS/code sandbox."""
    original = socket.socket

    class LocalSocket(original):
        def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
            if family in (socket.AF_INET, socket.AF_INET6) or (family == -1 and fileno is None):
                raise PermissionError("network forbidden in local embedding worker")
            super().__init__(family, type, proto, fileno)

    socket.socket = LocalSocket


def verify_model_files(directory):
    root = Path(directory).absolute()
    if not root.is_dir():
        raise ValueError("model directory missing")
    for name, (size, expected, kind) in MODEL_FILES.items():
        path = root / name
        if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)):
            raise ValueError("model path cannot contain symlinks or junctions")
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError("official model file missing or size differs: " + name)
        digest = hashlib.sha256() if kind == "sha256" else hashlib.sha1()
        if kind == "git":
            digest.update(f"blob {size}\0".encode())
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError("official model file hash differs: " + name)
    return root


def query_text(text, action):
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}" if action == "search" else text


def last_token_pool(hidden, mask):
    if bool((mask[:, -1] == 1).all()):
        return hidden[:, -1]
    return hidden[__import__("torch").arange(hidden.shape[0], device=hidden.device), mask.sum(dim=1) - 1]


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--device", required=True, choices=["cuda:0"])
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--max-length", type=int, required=True)
    args = parser.parse_args()
    deny_internet_sockets()
    if not 1 <= args.batch_size <= 4 or not 64 <= args.max_length <= 1024:
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
        "network_guard": "python_inet_socket_denied_not_os_sandbox",
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
