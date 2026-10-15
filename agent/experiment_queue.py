"""Local one-job-per-GPU scheduling; only the coordinator writes queue state."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from paper_agent.project_config import load_compute_config, load_execution_config
from .experiment_executor import AwaitingResults, ExperimentExecutionError, job_dependencies


def discover_gpus(executor):
    """Return logical visible indices, physical visibility tokens and used MiB."""
    count = executor.available_devices()['cuda_count']
    if not count:
        return {}
    result = subprocess.run(
        ['nvidia-smi', '--query-gpu=index,uuid,memory.used', '--format=csv,noheader,nounits'],
        capture_output=True, text=True, timeout=10, check=True)
    physical = [tuple(part.strip() for part in row.split(','))
                for row in result.stdout.splitlines() if row.strip()]
    visible = os.environ.get('CUDA_VISIBLE_DEVICES')
    tokens = visible.split(',') if visible is not None else [row[0] for row in physical]
    devices = {}
    for logical, token in enumerate(tokens[:count]):
        token = token.strip()
        matches = [r for r in physical if token == r[0] or r[1].startswith(token)]
        if len(matches) != 1:
            raise ValueError(f'Cannot map visible CUDA device {token!r} to nvidia-smi')
        devices[logical] = dict(token=matches[0][1], memory_used_mib=int(matches[0][2]))
    return devices


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def run_queue(executor, state, manifest, *, on_progress=None):
    project = executor.project
    directory = project / '.paper-agent'
    directory.mkdir(exist_ok=True)
    with (directory / 'experiment-executor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AwaitingResults('Another experiment queue is already executing this project') from exc
        return _schedule(executor, state, manifest, on_progress)


def _schedule(executor, state, manifest, on_progress):
    jobs = manifest['jobs']
    dependencies = job_dependencies(jobs)
    config = load_execution_config(executor.project)
    compute = load_compute_config(executor.project)
    configured_ids = config['gpu_ids']
    initial = discover_gpus(executor)
    allowed = list(initial) if configured_ids == 'auto' else configured_ids
    if compute['gpu_index'] is not None:
        allowed = [i for i in allowed if i == compute['gpu_index']]
    if any(i not in initial for i in allowed):
        raise AwaitingResults(f'Configured GPU indices are unavailable: {allowed}')
    gpu_limit = len(allowed) if config['max_parallel_gpu_jobs'] == 'auto' else min(len(allowed), config['max_parallel_gpu_jobs'])
    # Existing total budget counts cumulative job seconds. Keep it exact by serializing
    # budgeted attempts rather than letting simultaneous jobs overspend a shared limit.
    budgeted = compute['total_experiment_seconds'] is not None
    prior = []
    for record in sorted((executor.project / 'experiment-runs').glob('*/status.json')):
        receipt = json.loads(record.read_text())
        if receipt.get('status') == 'running':
            pid = receipt.get('pid')
            if not pid or Path(f'/proc/{pid}').exists():
                raise AwaitingResults(f'Unfinished experiment receipt: {record}; verify the old process before resuming')
            receipt.update(status='interrupted', error='Queue restart: recorded process no longer exists')
            if 'finished' not in receipt:
                receipt['finished'] = datetime.now(timezone.utc).isoformat()
            if 'elapsed_seconds' not in receipt:
                receipt['elapsed_seconds'] = max(0, (datetime.fromisoformat(receipt['finished']) -
                                                       datetime.fromisoformat(receipt['started'])).total_seconds())
            atomic_json(record, receipt)
        if receipt.get('status') == 'completed':
            prior.append(receipt)
    if budgeted:
        executor.seconds_used()
    prior += state.experiment_completed_receipts or state.node_results.get('experiment_run', {}).get('jobs', [])
    completed = {}
    expected_jobs = {job['id']: job for job in jobs}
    for receipt in prior:
        job = receipt.get('job', {})
        identifier = job.get('id')
        # Dependency metadata can be added without invalidating identical measurements.
        execution = lambda value: {k: v for k, v in value.items() if k != 'depends_on'}
        if (receipt.get('status') != 'completed' or identifier not in expected_jobs
                or execution(job) != execution(expected_jobs[identifier])):
            continue
        try:
            for path in job['expected_outputs']:
                executor.validate_measurements(path)
        except (ValueError, RuntimeError, OSError):
            continue
        completed[identifier] = receipt
    # A dependent result cannot survive a missing/changed prerequisite.
    while True:
        invalid = [i for i in completed if any(d not in completed for d in dependencies[i])]
        if not invalid:
            break
        for i in invalid:
            del completed[i]
    pending = {job['id']: job for job in jobs if job['id'] not in completed}
    running = {}
    retries = {}
    retry_after = {}
    failure = None
    failures = {}
    gpu_locks = {}
    cancel = Event()
    queue_path = executor.project / 'experiment-queue-state.json'
    tick = 0

    def checkpoint():
        nonlocal tick
        receipts = [completed[j['id']] for j in jobs if j['id'] in completed]
        state.experiment_completed_receipts = receipts
        state.node_results['experiment_run'] = dict(artifact='experiment-runs', phase='experiment_run',
                                                   jobs=receipts, deferred_jobs=manifest.get('deferred_jobs', []))
        active = {job['id']: gpu for job, gpu in running.values()}
        blocked = set(failures)
        while True:
            more = {i for i, deps in dependencies.items() if any(d in blocked for d in deps)} - blocked
            if not more:
                break
            blocked |= more
        atomic_json(queue_path, dict(
            status='failed' if failure else 'running' if pending or running else 'completed',
            error=str(failure) if failure else None, available_gpu_ids=allowed,
            max_parallel_gpu_jobs=gpu_limit, budget_serialized=budgeted,
            jobs=[dict(id=j['id'], depends_on=dependencies[j['id']],
                       status='completed' if j['id'] in completed else 'running' if j['id'] in active else
                       'failed' if j['id'] in failures else 'blocked' if j['id'] in blocked else 'pending',
                       error=failures.get(j['id']),
                       gpu_index=active.get(j['id']), oom_retries=retries.get(j['id'], 0)) for j in jobs]))
        tick += 1
        if on_progress:
            on_progress('experiment_run', state,
                                   dict(round=tick, stage=f"queue: {len(completed)} completed, {len(running)} running, {len(pending)} pending"))

    checkpoint()
    try:
        with ThreadPoolExecutor(max_workers=max(1, gpu_limit) + 1) as pool:
            try:
                while pending or running:
                    for future in list(running):
                        if not future.done():
                            continue
                        job, gpu = running.pop(future)
                        if gpu in gpu_locks:
                            gpu_locks.pop(gpu).close()
                        try:
                            completed[job['id']] = future.result()
                        except Exception as exc:
                            # Only CUDA OOM gets an automatic queue retry.
                            oom = isinstance(exc, ExperimentExecutionError) and ('CUDA out of memory' in str(exc) or 'torch.OutOfMemoryError' in str(exc))
                            if oom and retries.get(job['id'], 0) < config['oom_max_retries'] and failure is None:
                                retries[job['id']] = retries.get(job['id'], 0) + 1
                                retry_after[job['id']] = time.monotonic() + config['oom_retry_delay_seconds']
                                pending[job['id']] = job
                            else:
                                failure = failure or exc
                                failures[job['id']] = str(exc)
                        checkpoint()
                    if failure:
                        # Drain already-running jobs before returning to the workflow repair path.
                        if not running:
                            raise failure
                    else:
                        ready = [j for j in pending.values() if all(d in completed for d in dependencies[j['id']])
                                 and time.monotonic() >= retry_after.get(j['id'], 0)]
                        devices = discover_gpus(executor) if ready else {}
                        occupied = {gpu for _, gpu in running.values() if gpu is not None}
                        free = [i for i in allowed if i not in occupied and i in devices
                                and devices[i]['memory_used_mib'] < config['gpu_idle_memory_threshold_mib']]
                        for job in ready:
                            if budgeted and running:
                                break
                            try:
                                resources = executor.resolve_resources(job)
                            except Exception as exc:
                                failure = exc
                                failures[job['id']] = str(exc)
                                checkpoint()
                                break
                            gpu = None
                            if resources['allocated_device'] == 'cuda':
                                if not allowed:
                                    failure = AwaitingResults('No visible CUDA GPUs available for the experiment queue')
                                    break
                                if len(occupied) >= gpu_limit or not free:
                                    continue
                                lock_dir = Path.home() / '.cache' / 'paper-agent' / 'gpu-locks'
                                lock_dir.mkdir(parents=True, exist_ok=True)
                                while free:
                                    candidate = free.pop(0)
                                    gpu_lock = (lock_dir / f"{devices[candidate]['token']}.lock").open('a')
                                    try:
                                        fcntl.flock(gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                    except BlockingIOError:
                                        gpu_lock.close()
                                        continue
                                    gpu = candidate
                                    gpu_locks[gpu] = gpu_lock
                                    break
                                if gpu is None:
                                    continue
                                occupied.add(gpu)
                                resources = resources | dict(gpu_index=gpu, cuda_visible_device=devices[gpu]['token'])
                            elif any(g is None for _, g in running.values()):
                                continue  # One CPU/MPS job at a time.
                            future = pool.submit(executor.execute, job, resources=resources, cancel_event=cancel)
                            running[future] = (job, gpu)
                            del pending[job['id']]
                            checkpoint()
                    if pending or running:
                        time.sleep(config['poll_seconds'])
            except BaseException:
                cancel.set()
                raise
    except BaseException as exc:
        failure = failure or exc
        # Executor has drained/terminated its workers; persist their final outcomes.
        for future, (job, gpu) in list(running.items()):
            try:
                completed[job['id']] = future.result()
            except BaseException as worker_error:
                failures[job['id']] = str(worker_error)
        running.clear()
        for handle in gpu_locks.values():
            handle.close()
        gpu_locks.clear()
        checkpoint()
        raise
    checkpoint()
    return [completed[j['id']] for j in jobs]
