"""Choose a transport without changing the coordinator's stage contract."""
import os

from .workflow_cli import execute_stage


def dispatch_stage(project, node, instruction, **options):
    from .herdr import connect, ensure_no_pending, load_config, submit
    ensure_no_pending(project)
    config = load_config(project)
    # A Web server started inside Herdr can bind newly created research projects.
    # An explicit project binding always takes precedence over this launch default.
    session = os.environ.get('PAPER_AGENT_HERDR_SESSION')
    if config is None and session:
        config = connect(project, session)
    if config and node in config['nodes']:
        return submit(project, node, instruction, **options)
    return execute_stage(project, node, instruction, **options)
