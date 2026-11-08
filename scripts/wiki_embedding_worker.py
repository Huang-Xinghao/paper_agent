"""Local E5 ONNX encoder, run in an existing environment with numpy/tokenizers/ORT.

No downloads are performed here. Model files must be prepared explicitly.
"""

import argparse
import json
from pathlib import Path


def main():
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_file(str(args.model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(
        str(args.model_dir / "model.onnx"),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    vectors, lengths = [], []
    for start in range(0, len(payload["texts"]), 12):
        encoded = tokenizer.encode_batch(payload["texts"][start : start + 12])
        feed = {
            "input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.asarray(
                [e.attention_mask for e in encoded], dtype=np.int64
            ),
            "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64),
        }
        required = {i.name for i in session.get_inputs()}
        outputs = session.run(None, {k: v for k, v in feed.items() if k in required})
        hidden = outputs[0]
        if hidden.ndim != 3:
            raise ValueError("Expected E5 last_hidden_state, got incompatible model")
        mask = feed["attention_mask"][..., None]
        pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1)
        pooled /= np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
        vectors.extend(pooled.tolist())
        lengths.extend(feed["attention_mask"].sum(axis=1).tolist())
    args.output.write_text(
        json.dumps({"vectors": vectors, "token_lengths": lengths}), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
