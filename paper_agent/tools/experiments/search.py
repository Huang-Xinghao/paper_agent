"""Budgeted successive halving inside experiment_bridge, using the normal executor."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import fcntl
import json
import math
from pathlib import Path
import random
import re
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paper_agent.tools.experiments.executor import AwaitingResults, ExperimentExecutionError, ExperimentExecutor
from paper_agent.tools.experiments.queue import recover_receipts, run_queue
from paper_agent.storage import write_json as atomic_json


PLAN = 'refine-logs/EXPERIMENT_PLAN.md'
SPEC = 'experiments/hpo_spec.json'
BEST = 'results/hpo/best_config.json'
VERSION = 'successive-halving-v1'


class Parameter(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['categorical', 'int', 'float']
    choices: list[Any] = Field(default_factory=list)
    low: float | None = Field(default=None, allow_inf_nan=False)
    high: float | None = Field(default=None, allow_inf_nan=False)
    log: bool = False

    @model_validator(mode='after')
    def domain(self):
        if self.type == 'categorical':
            if not self.choices or any(type(v) not in (str, int, float, bool) or
                    isinstance(v, float) and not math.isfinite(v) for v in self.choices):
                raise ValueError('categorical choices require finite JSON scalar values')
        elif (self.low is None or self.high is None or self.low > self.high
              or self.log and self.low <= 0
              or self.type == 'int' and (not self.low.is_integer() or not self.high.is_integer())):
            raise ValueError('invalid search bounds')
        return self


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    reason: str = ''
    model_structure: str = ''
    fixed_parameters: dict[str, Any] = Field(default_factory=dict)
    search_space: dict[str, Parameter] = Field(default_factory=dict)
    metric: str = 'validation.mae'
    direction: Literal['minimize', 'maximize'] = 'minimize'
    validation_protocol: str = ''
    scope: str = ''
    candidates: int = Field(default=12, ge=1)
    rungs: list[int] = Field(default_factory=lambda: [5, 20, 60])
    reduction_factor: int = Field(default=3, ge=2)
    max_seconds: float = Field(default=3600, gt=0, allow_inf_nan=False)
    trial_timeout_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    sampler_seed: int = 42
    seeds: list[int] = Field(default_factory=lambda: [17, 23])
    smoke_epochs: int = Field(default=1, ge=1)

    @model_validator(mode='after')
    def experiment(self):
        if not self.enabled:
            if not self.reason.strip():
                raise ValueError('disabled HPO needs a reason')
            return self
        if not self.model_structure.strip() or not self.validation_protocol.strip() or not self.scope.strip():
            raise ValueError('HPO needs model_structure, validation_protocol and scope')
        if not self.metric.startswith('validation.') or not self.metric.removeprefix('validation.'):
            raise ValueError('HPO objective must be a validation.* metric')
        if (not self.search_space or set(self.fixed_parameters) & set(self.search_space)
                or any(not re.fullmatch(r'[A-Za-z][\w.-]*', k) for k in
                       [*self.fixed_parameters, *self.search_space])):
            raise ValueError('search parameters must be named and distinct from fixed parameters')
        if (not self.rungs or any(type(v) is not int or v < 1 for v in self.rungs)
                or self.rungs != sorted(set(self.rungs)) or self.smoke_epochs > self.rungs[0]):
            raise ValueError('rungs must be increasing positive epoch counts, after smoke_epochs')
        if not self.seeds or len(set(self.seeds)) != len(self.seeds):
            raise ValueError('HPO seeds must be nonempty and distinct')
        return self


class SearchSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    block_id: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    selection: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    code_files: list[str] = Field(min_length=1)
    required_inputs: list[str] = Field(default_factory=list)
    resources: dict[str, str] = Field(default_factory=lambda: {'device': 'auto'})

    @model_validator(mode='after')
    def command(self):
        command = ' '.join(self.argv)
        if '{config}' not in command or '{output_dir}' not in command:
            raise ValueError('HPO argv must pass {config} and {output_dir} to the training entrypoint')
        if set(self.resources) - {'device'} or self.resources.get('device', 'auto') not in {'auto', 'cpu', 'cuda', 'mps'}:
            raise ValueError('HPO resources support only device')
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_policy(project, *, path=PLAN) -> Policy | None:
    document = Path(project) / path
    return parse_policy(document.read_text(encoding='utf-8')) if document.is_file() else None


def parse_policy(text: str) -> Policy | None:
    matches = re.findall(r'^```hpo-policy\s*\n(.*?)^```\s*$', text, re.M | re.S)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('Policy document must have one hpo-policy block')
    return Policy.model_validate(json.loads(matches[0]))


def approved_policy(project, state):
    policy, relative = read_policy(project), PLAN
    if policy is None or not policy.enabled:
        return None
    source_sha = hashlib.sha256((Path(project) / relative).read_bytes()).hexdigest()
    node = 'experiment_plan'
    accepted_events = {'expert_approved', 'auto_selected'} if state.mode == 'auto' else {'expert_approved'}
    approval = next((e for e in reversed(state.history)
                     if e.get('event') in accepted_events and e.get('node') == node), {})
    if approval.get('sha256') != source_sha:
        raise ValueError('搜索范围和预算需随当前实验计划确认后再寻优')
    return policy


def read_spec(project):
    return SearchSpec.model_validate_json((Path(project) / SPEC).read_text())


def _identity(executor, policy, spec):
    fingerprints = {}
    for name in spec.code_files:
        path = executor.output_path(name)
        if not path.is_file():
            raise ValueError(f'HPO code/split configuration missing: {name}')
        fingerprints[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Large battery datasets use file metadata; code and split files above use content hashes.
    for name in spec.required_inputs:
        path = executor.input_path(name)
        if not path.exists():
            raise AwaitingResults(f'Missing HPO input: {name}')
        files = [path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
        fingerprints[f'input:{name}'] = digest([
            [str(p.relative_to(path)) if p != path else '.', p.stat().st_size, p.stat().st_mtime_ns]
            for p in files])
    settings = policy.model_dump(exclude={'max_seconds'})
    return digest([VERSION, settings, spec.model_dump(), fingerprints]), fingerprints


def _candidates(policy):
    rng = random.Random(policy.sampler_seed)
    candidates = []
    seen = set()
    for _ in range(max(1000, policy.candidates * 100)):
        parameters = deepcopy(policy.fixed_parameters)
        for key, space in policy.search_space.items():
            if space.type == 'categorical':
                value = rng.choice(space.choices)
            elif space.type == 'int' and not space.log:
                value = rng.randint(int(space.low), int(space.high))
            else:
                value = math.exp(rng.uniform(math.log(space.low), math.log(space.high))) if space.log else rng.uniform(space.low, space.high)
                if space.type == 'int':
                    value = max(int(space.low), min(int(space.high), round(value)))
            parameters[key] = value
        key = digest(parameters)
        if key not in seen:
            candidates.append(parameters)
            seen.add(key)
        if len(candidates) == policy.candidates:
            break
    return candidates


def _metric(path, key):
    value = json.loads(path.read_text())
    for part in key.split('.'):
        value = value[part]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'Nonfinite/non-numeric selection metric: {path}: {key}')
    return value


class SearchExecutor(ExperimentExecutor):
    # Budget counts cumulative process seconds; serial trials prevent overspending.
    serial_budget = True

    def __init__(self, executor, limit):
        super().__init__(executor.project, knowledge_root=executor.knowledge_root)
        self.limit = limit
        self.available_devices = executor.available_devices

    def search_seconds(self):
        total = 0.0
        for receipt in recover_receipts(self):
            if receipt.get('job', {}).get('purpose') != 'hpo':
                continue
            total += receipt['elapsed_seconds']
        return total

    def execute(self, job, **kwargs):
        remaining = self.limit - self.search_seconds()
        if remaining <= 0:
            raise AwaitingResults('HPO budget exhausted')
        # Keep job identity stable across resumes; only the effective timeout shrinks.
        resources = deepcopy(kwargs.pop('resources', None) or self.resolve_resources(job))
        resources['requested']['hpo_remaining_seconds'] = remaining
        return super().execute(job, resources=resources, **kwargs)


def run_search(executor, state, *, on_progress=None):
    """Run smoke + halving + final seed confirmation; return one reproducible selection."""
    directory = executor.project / '.paper-agent'
    directory.mkdir(exist_ok=True)
    with (directory / 'hpo.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AwaitingResults('Another hyperparameter search is already running') from exc
        return _run_search(executor, state, on_progress=on_progress)


def _run_search(executor, state, *, on_progress=None):
    project = executor.project
    policy = approved_policy(project, state)
    if policy is None:
        if (project / SPEC).exists() and read_policy(project) is None:
            raise ValueError('hpo_spec.json requires an enabled, confirmed hpo-policy')
        return {'status': 'disabled'}
    spec = read_spec(project)
    identity, fingerprints = _identity(executor, policy, spec)
    root = project / 'results/hpo'
    root.mkdir(parents=True, exist_ok=True)
    directory = root / identity[:16]
    directory.mkdir(exist_ok=True)
    search_executor = SearchExecutor(executor, policy.max_seconds)
    search_state = SimpleNamespace(experiment_completed_receipts=[])
    search = {'version': VERSION, 'search_id': identity, 'status': 'running',
              'policy': policy.model_dump(), 'spec': spec.model_dump(), 'fingerprints': fingerprints,
              'candidates': _candidates(policy), 'trials': [], 'promotions': [],
              'training_strategy': 'restart_from_scratch_at_each_rung'}
    save_path = directory / 'search.json'
    best_path = root / 'best_config.json'
    if best_path.is_file() and save_path.is_file():
        previous = json.loads(best_path.read_text())
        record = json.loads(save_path.read_text())
        if (previous.get('search_id') == identity and record.get('status') == 'completed'
                and (not previous.get('budget_limited') or record['policy']['max_seconds'] == policy.max_seconds)
                and all((project / row['metrics']).is_file() for row in record['trials'])):
            return previous
    # Regenerate deterministic jobs and recover through durable execution receipts.
    # Failed/interrupted searches never treat a previous selection as current.
    (root / 'best_config.json').unlink(missing_ok=True)
    observations = {}

    def save():
        search['elapsed_seconds'] = search_executor.search_seconds()
        atomic_json(save_path, search)

    def trial(candidate, rung, seed, *, smoke=False):
        mode = 'smoke' if smoke else 'tuning'
        label = f'{mode}_c{candidate}_r{rung}_s{seed}'
        if on_progress:
            on_progress('experiment_bridge', state,
                        {'stage': f'hpo: {mode}, candidate {candidate}, rung {rung}, seed {seed}'})
        relative = directory.relative_to(project) / label
        output = project / relative
        output.mkdir(exist_ok=True)
        epochs = policy.smoke_epochs if smoke else policy.rungs[rung]
        configuration = {'parameters': search['candidates'][candidate], 'epochs': epochs,
                         'seed': seed, 'phase': mode, 'evaluation_split': 'validation'}
        config_path = output / 'config.json'
        atomic_json(config_path, configuration)
        substitutions = {'{config}': str(config_path.relative_to(project)), '{output_dir}': str(relative),
                         '{epochs}': str(epochs), '{seed}': str(seed), '{mode}': mode}
        argv = list(spec.argv)
        for placeholder, value in substitutions.items():
            argv = [arg.replace(placeholder, value) for arg in argv]
        job = {'id': f'hpo_{identity[:16]}_{label}', 'block_id': spec.block_id,
               'purpose': 'hpo', 'search_id': identity, 'depends_on': [], 'argv': argv,
               'resources': spec.resources, 'timeout_seconds': policy.trial_timeout_seconds,
               'input_fingerprints': {**fingerprints, str(config_path.relative_to(project)): digest(configuration)},
               'expected_outputs': [{'path': str(relative / 'metrics.json'),
                                     'metrics': [] if smoke else [policy.metric]}]}
        manifest = {'backend': 'local', 'required_inputs': spec.required_inputs, 'jobs': [job]}
        search_executor.validate_manifest(manifest)
        search_executor.validate_inputs(manifest)
        receipts = run_queue(search_executor, search_state, manifest, on_progress=on_progress,
                             track=False, queue_path=directory / 'queue.json')
        score = None if smoke else _metric(output / 'metrics.json', policy.metric)
        row = {'candidate': candidate, 'rung': rung, 'epochs': epochs, 'seed': seed,
               'phase': mode, 'score': score, 'config': str(config_path.relative_to(project)),
               'metrics': str(relative / 'metrics.json'), 'log': receipts[0]['log'],
               'elapsed_seconds': receipts[0]['elapsed_seconds']}
        search['trials'].append(row)
        if not smoke:
            observations[candidate, rung, seed] = score
        save()
        return score

    reached = []
    interrupted_by_budget = False
    try:
        save()
        trial(0, 0, policy.seeds[0], smoke=True)
        active = list(range(len(search['candidates'])))
        for rung in range(len(policy.rungs)):
            measured = []
            for candidate in active:
                trial(candidate, rung, policy.seeds[0])
                measured.append(candidate)
                reached = [(c, rung) for c in measured]
            reverse = policy.direction == 'maximize'
            ranked = sorted(active, key=lambda c: observations[c, rung, policy.seeds[0]], reverse=reverse)
            if rung < len(policy.rungs) - 1:
                active = ranked[:max(1, math.ceil(len(ranked) / policy.reduction_factor))]
                search['promotions'].append({'from_rung': rung, 'candidates': active})
                save()
        # Compare final candidates at the same epoch count and across the same seeds.
        for candidate, rung in reached:
            for seed in policy.seeds[1:]:
                trial(candidate, rung, seed)
    except (AwaitingResults, ExperimentExecutionError) as exc:
        if search_executor.search_seconds() < policy.max_seconds:
            search.update(status='awaiting_results' if isinstance(exc, AwaitingResults) else 'failed', error=str(exc))
            save()
            raise
        interrupted_by_budget = True
        search['budget_message'] = str(exc)
    except BaseException as exc:
        search.update(status='interrupted' if not isinstance(exc, Exception) else 'failed', error=str(exc))
        save()
        raise
    eligible = [(c, rung) for c, rung in reached
                if all((c, rung, seed) in observations for seed in policy.seeds)]
    if not eligible:
        search.update(status='awaiting_results', error='Budget ended before comparable candidates completed all selection seeds')
        save()
        raise AwaitingResults(search['error'] + '; increase the confirmed HPO budget or reduce the search design')
    score = lambda item: sum(observations[item[0], item[1], s] for s in policy.seeds) / len(policy.seeds)
    winner = (min if policy.direction == 'minimize' else max)(eligible, key=score)
    best = {'version': VERSION, 'search_id': identity, 'status': 'completed', 'phase': 'formal',
            'parameters': search['candidates'][winner[0]], 'metric': policy.metric,
            'direction': policy.direction, 'validation_score': score(winner),
            'epochs': policy.rungs[winner[1]], 'seeds': policy.seeds,
            'budget_limited': interrupted_by_budget, 'scope': policy.scope,
            'selection': spec.selection, 'search_record': str(save_path.relative_to(project))}
    search.update(status='completed', best=best)
    save()
    atomic_json(root / 'best_config.json', best)
    return best


def bind_best_config(executor, manifest, state):
    """Bind selected parameters into declared formal jobs and their reuse identity."""
    policy = approved_policy(executor.project, state)
    if policy is None:
        if manifest.get('hpo'):
            raise ValueError('Formal HPO jobs require a confirmed policy')
        return manifest
    binding = manifest.get('hpo', {})
    identifiers = binding.get('job_ids', [])
    jobs = {j['id']: j for j in manifest['jobs']}
    if not identifiers or any(i not in jobs for i in identifiers):
        raise ValueError('manifest.hpo.job_ids must identify formal jobs consuming best_config')
    spec = read_spec(executor.project)
    identity, fingerprints = _identity(executor, policy, spec)
    best_path = executor.project / BEST
    best = json.loads(best_path.read_text())
    if best.get('search_id') != identity or best.get('status') != 'completed':
        raise ValueError('Run hyperparameter search for the current policy/code before formal experiments')
    result = deepcopy(manifest)
    for job in result['jobs']:
        if job['id'] not in identifiers:
            continue
        if '{best_config}' not in job['argv']:
            raise ValueError(f"{job['id']}: argv must pass {{best_config}} as a configuration argument")
        job['argv'] = [BEST if arg == '{best_config}' else arg for arg in job['argv']]
        job['input_fingerprints'] = {**job.get('input_fingerprints', {}), **fingerprints,
                                     BEST: digest({key: best[key] for key in ('parameters', 'epochs')})}
    return result
