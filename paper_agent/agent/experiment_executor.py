"""Validate and execute local experiment jobs without model or workflow routing."""
from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_agent.project_config import load_compute_config, validate_job_timeout
from .experiment_handoff import read_plan_blocks


class AwaitingResults(Exception):
    """Execution needs data, resources or budget before it can continue."""


class ExperimentExecutionError(RuntimeError):
    """An executed job failed; its receipt and log are already durable."""

    def __init__(self, message, *, receipt=None):
        super().__init__(message)
        self.receipt = receipt


def job_dependencies(jobs):
    """Require explicit dependency lists; [] permits independent scheduling."""
    ids = {job['id'] for job in jobs}
    dependencies = {}
    for job in jobs:
        deps = job.get('depends_on')
        if (not isinstance(deps, list) or any(not isinstance(d, str) or d not in ids
                                             or d == job['id'] for d in deps)
                or len(set(deps)) != len(deps)):
            raise ValueError(f"Invalid depends_on for {job['id']}")
        dependencies[job['id']] = deps
    remaining = set(ids)
    while remaining:
        ready = {i for i in remaining if not (set(dependencies[i]) & remaining)}
        if not ready:
            raise ValueError('Experiment dependencies contain a cycle')
        remaining -= ready
    return dependencies


def receipt_matches_job(receipt, job):
    execution = lambda value: {k: v for k, v in value.items() if k not in {'depends_on', 'block_id'}}
    return receipt.get('status') == 'completed' and execution(receipt.get('job', {})) == execution(job)


class ExperimentExecutor:
    def __init__(self, project: Path, *, knowledge_root: Path | None = None):
        self.project = Path(project).resolve()
        self.knowledge_root = knowledge_root

    def output_path(self, relative: str) -> Path:
        path = (self.project / relative).resolve()
        if not path.is_relative_to(self.project):
            raise ValueError(f"experiment path escapes workspace: {relative}")
        return path

    def input_path(self, path: str) -> Path:
        from paper_agent.tools.data_access import resolve_data_read
        root = self.knowledge_root
        return resolve_data_read(self.project, (root,) if root else (), path)

    def load_manifest(self) -> dict[str, Any]:
        manifest = json.loads((self.project / "experiments/manifest.json").read_text())
        if manifest.get("backend") != "local":
            raise ValueError("experiment manifest backend must be local")
        jobs = manifest.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise ValueError("manifest requires nonempty jobs")
        seen = set()
        outputs = set()
        for job in jobs:
            requirement = job.get("resources", {})
            if not isinstance(requirement, dict) or set(requirement) - {"device"} or requirement.get("device", "auto") not in {"auto", "cpu", "cuda", "mps"}:
                raise ValueError("job resources must contain only device: auto, cpu, cuda or mps")
            identifier = job.get("id", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier) or identifier in seen:
                raise ValueError("job IDs must be unique simple identifiers")
            seen.add(identifier)
            argv = job.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise ValueError("job argv must be a nonempty string list")
            validate_job_timeout(self.project, job)
            expected = job.get("expected_outputs")
            if not isinstance(expected, list) or not expected:
                raise ValueError("job requires expected_outputs")
            for output in expected:
                if (not isinstance(output, dict) or set(output) - {"path", "metrics", "nonempty"}
                        or not isinstance(output.get("path"), str)):
                    raise ValueError(f"{identifier}: expected_outputs entries need path and optional metrics/nonempty")
                if 'nonempty' in output and not isinstance(output['nonempty'], bool):
                    raise ValueError(f"{identifier}: nonempty must be a boolean")
                path = self.output_path(output["path"])
                if not path.is_relative_to(self.project / "results"):
                    raise ValueError(f"{identifier}: outputs must be under results/: {output['path']}")
                metrics = output.get("metrics", [])
                if (not isinstance(metrics, list) or any(not isinstance(k, str) or not k for k in metrics)
                        or (metrics and path.suffix not in {".json", ".csv"})):
                    raise ValueError(f"{identifier}: metrics must name JSON/CSV numeric fields: {output['path']}")
                if str(path) in outputs:
                    raise ValueError(f"jobs must have distinct output paths: {output['path']}")
                outputs.add(str(path))
        blocks = {block['id'] for block in read_plan_blocks(self.project)}
        for job in jobs:
            block_id = job.get('block_id')
            if not isinstance(block_id, str) or block_id not in blocks:
                raise ValueError(f"{job['id']}: block_id must name a planned D/B block; got {block_id!r}")
        inputs = manifest.get("required_inputs")
        if not isinstance(inputs, list) or not inputs or not all(isinstance(p, str) for p in inputs):
            raise ValueError("manifest requires required_inputs")
        job_dependencies(jobs)
        return manifest

    def validate_inputs(self, manifest):
        """Check real inputs before starting the formal experiment queue."""
        self.compute_config()
        missing = [p for p in manifest['required_inputs'] if not self.input_path(p).exists()]
        if missing:
            raise AwaitingResults(f"Missing experiment inputs: {missing}. Supply real data and resume.")

    def compute_config(self) -> dict[str, Any]:
        return load_compute_config(self.project)

    @staticmethod
    def available_devices() -> dict[str, Any]:
        available = {"cpu": True, "cuda_count": 0, "mps": False}
        if importlib.util.find_spec("torch") is None:
            return available
        try:
            import torch
            if torch.cuda.is_available():
                available["cuda_count"] = torch.cuda.device_count()
            available["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        except (ImportError, OSError, RuntimeError):
            pass
        return available

    def resolve_resources(self, job: dict[str, Any]) -> dict[str, Any]:
        config = self.compute_config()
        required = job.get("resources", {}).get("device", "auto")
        selected = config["device"]
        if selected != "auto" and required != "auto" and selected != required:
            raise AwaitingResults(
                f"Experiment {job['id']} requires {required}, but project compute configuration selects {selected}. "
                "Change the resource configuration or use a matching machine, then resume.")
        available = self.available_devices()
        device = required if required != "auto" else selected
        if device == "auto":
            device = "cuda" if config["gpu_index"] is not None or available["cuda_count"] else "mps" if available["mps"] else "cpu"
        index = config["gpu_index"] if device == "cuda" else None
        if device == "cuda" and (not available["cuda_count"] or index is not None and index >= available["cuda_count"]):
            raise AwaitingResults(
                f"Experiment {job['id']} requires CUDA"
                f"{f' GPU {index}' if index is not None else ''}; this Python environment reports "
                f"{available['cuda_count']} CUDA devices. Use a matching machine or change project-config.json, then resume.")
        if device == "mps" and not available["mps"]:
            raise AwaitingResults(
                f"Experiment {job['id']} requires MPS, but it is unavailable in this Python environment. "
                "Use a matching Mac or change project-config.json, then resume.")
        return {"requested": config, "required_device": required, "allocated_device": device,
                "gpu_index": index, "cpu_threads": config["cpu_threads"], "available": available}

    def output_issues(self, job):
        """Validate only declared artifacts and numeric fields, never guess from metadata."""
        issues = []
        for output in job['expected_outputs']:
            name = output['path']
            try:
                path = self.output_path(name)
                if not path.is_file() or not path.stat().st_size:
                    raise ValueError('file missing or empty')
                value = None
                if path.suffix == '.json':
                    value = json.loads(path.read_text())
                elif path.suffix == '.csv':
                    with path.open(newline='') as stream:
                        value = list(csv.DictReader(stream))
                if path.suffix in {'.json', '.csv'}:
                    if not isinstance(value, (dict, list)):
                        raise ValueError('expected a JSON object/array or CSV table')
                    if output.get('nonempty', False) and not value:
                        raise ValueError('expected nonempty content')
                for field in output.get('metrics', []):
                    def values(item, parts):
                        if isinstance(item, list):
                            return [v for row in item for v in values(row, parts)]
                        if not parts:
                            return [item]
                        if not isinstance(item, dict) or parts[0] not in item:
                            raise ValueError(f'missing metric field: {field}')
                        return values(item[parts[0]], parts[1:])
                    numbers = values(value, field.split('.'))
                    try:
                        valid = numbers and all(not isinstance(v, bool) and math.isfinite(float(v)) for v in numbers)
                    except (ValueError, TypeError, OverflowError):
                        valid = False
                    if not valid:
                        raise ValueError(f'metric {field} is empty or contains nonfinite/non-numeric values')
            except (OSError, ValueError, TypeError, OverflowError) as exc:
                issues.append(dict(job_id=job['id'], path=name, problem=str(exc),
                                   action='Fix the producer or its declared output, then run_experiments.'))
        return issues

    def validate_outputs(self, job):
        issues = self.output_issues(job)
        if issues:
            raise ValueError(json.dumps({'issues': issues}, ensure_ascii=False))

    def seconds_used(self) -> float:
        total = 0.0
        for path in (self.project / "experiment-runs").glob("*/status.json"):
            receipt = json.loads(path.read_text())
            if receipt.get("status") == "running":
                raise AwaitingResults(f"Unfinished experiment receipt: {path}. Reconcile the interrupted job before resuming a budgeted run.")
            elapsed = receipt.get("elapsed_seconds")
            if elapsed is None:
                elapsed = (datetime.fromisoformat(receipt["finished"]) -
                           datetime.fromisoformat(receipt["started"])).total_seconds()
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError(f"Invalid experiment duration: {path}")
            total += elapsed
        return total

    def execute(self, job: dict[str, Any], *, resources=None, cancel_event=None) -> dict[str, Any]:
        resources = resources if resources is not None else self.resolve_resources(job)
        config = resources["requested"]
        timeout = job["timeout_seconds"]
        validate_job_timeout(self.project, job)
        used = self.seconds_used() if config["total_experiment_seconds"] is not None else 0.0
        if config["total_experiment_seconds"] is not None:
            remaining = config["total_experiment_seconds"] - used
            if remaining <= 0:
                raise AwaitingResults("Project total_experiment_seconds exhausted; increase the user-owned budget to continue.")
            timeout = min(timeout, remaining)
        directory = self.project / "experiment-runs" / (job["id"] + "-" + uuid.uuid4().hex[:12])
        directory.mkdir(parents=True)
        # Preserve previous outputs, so stale metrics cannot count as this attempt.
        for index, output in enumerate(job["expected_outputs"]):
            path = self.output_path(output["path"])
            if path.exists():
                shutil.move(str(path), str(directory / f"previous-{index}{path.suffix}"))
        argv = list(job["argv"])
        if argv[0] in {"python", "python3"}: argv[0] = sys.executable
        env = os.environ.copy()
        env["PAPER_AGENT_DEVICE"] = resources["allocated_device"]
        backend_report = directory / "compute-backend.json"
        env["PAPER_AGENT_BACKEND_REPORT"] = str(backend_report.resolve())
        if resources["allocated_device"] == "cuda" and resources["gpu_index"] is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(resources.get("cuda_visible_device", resources["gpu_index"]))
        elif resources["allocated_device"] != "cuda":
            env["CUDA_VISIBLE_DEVICES"] = ""
        if resources["allocated_device"] == "mps":
            env["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
        if resources["cpu_threads"] is not None:
            env["OMP_NUM_THREADS"] = str(resources["cpu_threads"])
            env["MKL_NUM_THREADS"] = str(resources["cpu_threads"])
        receipt = {"job": job, "status": "running", "argv": argv, "resources": resources,
                   "compute_backend": {"status": "unknown", "reason": "No runtime backend report"},
                   "started": datetime.now(timezone.utc).isoformat(),
                   "log": str((directory / "output.log").relative_to(self.project))}
        record = directory / "status.json"
        receipt.update(effective_timeout_seconds=timeout, prior_experiment_seconds=used)
        record.write_text(json.dumps(receipt, indent=2))
        attempt_started = time.monotonic()
        try:
            with (directory / "output.log").open("w") as log:
                process = subprocess.Popen(argv, cwd=self.project, env=env, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                receipt["pid"] = process.pid
                record.write_text(json.dumps(receipt, indent=2))
                try:
                    while True:
                        if cancel_event is not None and cancel_event.is_set():
                            raise RuntimeError("Experiment queue interrupted")
                        remaining = timeout - (time.monotonic() - attempt_started)
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        try:
                            code = process.wait(timeout=min(1, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
            receipt["exit_code"] = code
            if code != 0: raise RuntimeError(f"exit code {code}")
            self.validate_outputs(job)
            receipt["status"] = "completed"
        except BaseException as exc:
            receipt.update(status="interrupted" if not isinstance(exc, Exception) or
                           cancel_event is not None and cancel_event.is_set() else "failed", error=str(exc))
            if not isinstance(exc, Exception):
                raise
        finally:
            if backend_report.is_file():
                try:
                    backend = json.loads(backend_report.read_text(encoding="utf-8"))
                    if (not isinstance(backend, dict)
                            or backend.get("device") not in {"cpu", "cuda", "mps", "mixed"}
                            or not isinstance(backend.get("framework"), str)
                            or not backend["framework"].strip()
                            or not isinstance(backend.get("evidence"), str)
                            or not backend["evidence"].strip()):
                        raise ValueError("backend report requires device, framework and runtime evidence")
                    receipt["compute_backend"] = {
                        "status": "reported", "source": "experiment_process",
                        "report": str(backend_report.relative_to(self.project)),
                        "device": backend["device"], "framework": backend["framework"],
                        "evidence": backend["evidence"],
                    }
                except (ValueError, OSError) as exc:
                    receipt["compute_backend"] = {"status": "unknown", "reason": f"Invalid runtime backend report: {exc}"}
            receipt["elapsed_seconds"] = time.monotonic() - attempt_started
            receipt["finished"] = datetime.now(timezone.utc).isoformat()
            record.write_text(json.dumps(receipt, indent=2))
        if receipt["status"] != "completed":
            log_tail = (directory / "output.log").read_text(errors="replace")[-12000:]
            if re.search(r"(?:HTTP(?:Error)?[ :]+|status(?: code)?[ :]+)(?:401|403)\b", log_tail, re.I):
                raise RuntimeError(f"Experiment authorization failure; log: {receipt['log']}")
            oom = " CUDA out of memory" if re.search(r"CUDA out of memory|torch\.OutOfMemoryError", log_tail) else ""
            raise ExperimentExecutionError(
                f"Experiment {job['id']} failed: {receipt.get('error')};{oom}; log: {receipt['log']}",
                receipt=receipt)
        return receipt
