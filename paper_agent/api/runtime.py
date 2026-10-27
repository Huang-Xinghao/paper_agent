"""Match the existing run-workflow runtime contract before launching research."""

import sys


def check_runtime():
    import torch

    print(f"Python: {sys.executable}", flush=True)
    print(
        f"PyTorch: {torch.__version__}; CUDA: {torch.version.cuda}; visible GPUs: {torch.cuda.device_count()}",
        flush=True,
    )
    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA unavailable. Fix environment/device visibility before starting research."
        )
    if (torch.ones(1, device="cuda") + 1).item() != 2:
        raise RuntimeError("CUDA computation check failed")
    print("CUDA computation check passed.", flush=True)
