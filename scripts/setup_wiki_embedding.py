"""Download only the pinned E5 ONNX encoder/tokenizer into an explicitly chosen directory.

Run with a Python environment that already has huggingface_hub. No dependency installation.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument(
        "--python",
        help="Existing Python with onnxruntime/tokenizers; writes runtime.env for start-local",
    )
    args = parser.parse_args()
    root = args.destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(root / "cache")
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    from huggingface_hub import hf_hub_download

    revision = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    files = {
        "onnx/model_qint8_avx512_vnni.onnx": "model.onnx",
        "tokenizer.json": "tokenizer.json",
    }
    hashes = {}
    for remote, name in files.items():
        target = root / name
        downloaded = Path(
            hf_hub_download(
                "intfloat/multilingual-e5-small",
                filename=remote,
                revision=revision,
                cache_dir=str(root / "cache"),
                local_dir=str(root / "download"),
            )
        )
        digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError(
                    f"Existing {target} differs; preserve it and choose a new destination"
                )
        else:
            downloaded.replace(target)
        hashes[name] = digest
        print(
            json.dumps(
                {"file": name, "bytes": target.stat().st_size, "sha256": digest}
            ),
            flush=True,
        )
    identity = {
        "model": "intfloat/multilingual-e5-small",
        "revision": revision,
        "format": "onnx/model_qint8_avx512_vnni.onnx",
        "pooling": "attention-mask mean; L2 normalize",
        "prefixes": ["query: ", "passage: "],
        "max_length": 512,
        "files": hashes,
    }
    (root / "identity.json").write_text(
        json.dumps(identity, indent=2), encoding="utf-8"
    )
    if args.python:
        (root / "runtime.env").write_text(
            f"PAPER_AGENT_EMBEDDING_DIR={root}\nPAPER_AGENT_EMBEDDING_PYTHON={args.python}\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
