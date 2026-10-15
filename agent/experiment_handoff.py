"""Check the bridge's plan-to-job handoff against real execution receipts."""
from __future__ import annotations

import json

from .loop import ArtifactValidationError


def validate_coverage(executor, state, manifest, *, completed=False):
    path = executor.project / 'experiments/coverage.json'
    try:
        entries = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ArtifactValidationError(f'Write experiments/coverage.json: {exc}') from exc
    if not isinstance(entries, list) or not entries:
        raise ArtifactValidationError('coverage.json must be a nonempty list of plan requirements')
    jobs = {job['id']: job for job in manifest['jobs']}
    receipts = {r.get('job', {}).get('id'): r for r in state.experiment_completed_receipts}
    seen = set()
    gaps = []
    for item in entries:
        if not isinstance(item, dict):
            raise ArtifactValidationError('Each coverage entry must be an object')
        identifier = item.get('id')
        if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
            raise ArtifactValidationError('Coverage requirements need unique nonempty IDs')
        seen.add(identifier)
        if not isinstance(item.get('plan_ref'), str) or not item['plan_ref'].strip():
            raise ArtifactValidationError(f'{identifier}: plan_ref must identify the original requirement')
        if type(item.get('required')) is not bool:
            raise ArtifactValidationError(f'{identifier}: required must be boolean')
        status = item.get('status')
        if status not in {'planned', 'completed', 'blocked', 'skipped'}:
            raise ArtifactValidationError(f'{identifier}: invalid status')
        if status in {'blocked', 'skipped'}:
            if not isinstance(item.get('reason'), str) or not item['reason'].strip():
                raise ArtifactValidationError(f'{identifier}: blocked/skipped requires a reason')
            if completed and status == 'blocked' and item['required']:
                gaps.append(identifier)
            if status == 'skipped' and item['required']:
                # A conditional skip needs the measured gate artifact, not just prose.
                evidence = item.get('evidence', [])
                if not isinstance(evidence, list) or not evidence:
                    raise ArtifactValidationError(f'{identifier}: required skip needs gate evidence paths')
                for name in evidence:
                    executor.validate_measurements(name)
                    if completed:
                        owners = [job_id for job_id, job in jobs.items()
                                  if name in job['expected_outputs']]
                        if not any(receipts.get(j, {}).get('status') == 'completed' and
                                   receipts[j].get('job') == jobs[j] for j in owners):
                            raise ArtifactValidationError(f'{identifier}: gate evidence needs a completed job receipt')
            continue
        job_ids, outputs = item.get('job_ids'), item.get('outputs')
        if (not isinstance(job_ids, list) or not job_ids or
                any(not isinstance(j, str) or j not in jobs for j in job_ids)):
            raise ArtifactValidationError(f'{identifier}: map to concrete manifest job_ids')
        expected = {p for j in job_ids for p in jobs[j]['expected_outputs']}
        if (not isinstance(outputs, list) or not outputs or
                any(not isinstance(p, str) or p not in expected for p in outputs)):
            raise ArtifactValidationError(f'{identifier}: outputs must be checked outputs of its jobs')
        if completed:
            if status != 'completed' and item['required']:
                gaps.append(identifier)
            for j in job_ids:
                receipt = receipts.get(j, {})
                if receipt.get('status') != 'completed' or receipt.get('job') != jobs[j]:
                    raise ArtifactValidationError(f'{identifier}: no completed receipt for current job {j}')
            for name in outputs:
                executor.validate_measurements(name)
    if gaps:
        raise ArtifactValidationError('Required experiment evidence remains incomplete: ' + ', '.join(gaps))
    return entries
