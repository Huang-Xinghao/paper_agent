"""Typed resource configuration and diagnostics for the experiment agent."""
import json
import os
from typing import Literal

from paper_agent.project_config import load_compute_config, load_execution_config, update_resources
from .core import ToolContext, ToolResult, tool


@tool()
def experiment_resources(ctx: ToolContext, action: Literal['inspect', 'configure'] = 'inspect',
                         device: Literal['auto', 'cpu', 'cuda', 'mps'] | None = None,
                         gpu_ids: list[int] | None = None, cpu_threads: int | None = None,
                         budget_hours: float | None = None, max_job_seconds: float | None = None,
                         max_parallel_gpu_jobs: int | None = None,
                         gpu_idle_memory_threshold_mib: int | None = None) -> ToolResult:
    """Inspect resources or configure them using the task's agreed resource budget.

    Configure maps parameters into validated project-config.json fields; omitted
    values retain the existing policy. budget_hours is cumulative job wall time,
    including failed attempts. Use device=auto for mixed CPU/CUDA jobs, gpu_ids to
    choose visible logical GPUs, and max_parallel_gpu_jobs for concurrency.
    gpu_idle_memory_threshold_mib controls shared-GPU admission; inspect usage
    before choosing it. Configuration applies to the next queue invocation.
    inspect returns actual CUDA availability, NVIDIA management status and policy.
    """
    if action not in {'inspect', 'configure'}:
        raise ValueError('action must be inspect or configure')
    changes = dict(device=device, gpu_ids=gpu_ids, cpu_threads=cpu_threads,
                   budget_hours=budget_hours, max_job_seconds=max_job_seconds,
                   max_parallel_gpu_jobs=max_parallel_gpu_jobs,
                   gpu_idle_memory_threshold_mib=gpu_idle_memory_threshold_mib)
    if action == 'inspect' and any(v is not None for v in changes.values()):
        raise ValueError('Use action=configure to apply resource parameters')
    if action == 'configure':
        value = update_resources(ctx.workspace, **changes)
        value['applies_to'] = 'next_queue_invocation'
    else:
        from .experiments.executor import ExperimentExecutor
        from .experiments.queue import discover_gpus
        executor = ExperimentExecutor(ctx.workspace)
        value = {'compute': load_compute_config(ctx.workspace),
                 'execution': load_execution_config(ctx.workspace),
                 'cpu_count': os.cpu_count(), 'available_devices': executor.available_devices()}
        try:
            value['gpus'] = discover_gpus(executor)
            value['gpu_discovery_error'] = None
        except (OSError, RuntimeError, ValueError) as exc:
            value['gpus'] = {}
            value['gpu_discovery_error'] = str(exc)
    return ToolResult(json.dumps(value, ensure_ascii=False), structured_content=value)
