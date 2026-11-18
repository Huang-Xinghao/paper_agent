import asyncio

from paper_agent.tools.core import ToolContext, bash


def test_failed_generation_reports_redirected_log_and_stops_conversion(tmp_path):
    result = asyncio.run(bash(ToolContext(tmp_path),
        "sh -c 'echo LaTeX-error-missing-dollar; exit 7' > build.log 2>&1 "
        "&& touch preview-1.png"))
    assert result.is_error
    assert result.metadata['returncode'] == 7
    assert 'exit code 7' in result.output
    assert 'Log tail (build.log)' in result.output
    assert 'LaTeX-error-missing-dollar' in result.output
    assert 'before view_image' in result.output
    assert not (tmp_path / 'preview-1.png').exists()


def test_redirected_log_uses_command_cwd_and_quoted_path(tmp_path):
    (tmp_path / 'figures').mkdir()
    result = asyncio.run(bash(ToolContext(tmp_path),
        "sh -c 'echo render-error; exit 2' > 'build output.log'", cwd='figures'))
    assert result.is_error
    assert 'Log tail (figures/build output.log)' in result.output
    assert 'render-error' in result.output


def test_failure_does_not_report_stale_or_external_logs(tmp_path):
    (tmp_path / 'stale.log').write_text('STALE-CONTENT')
    external = tmp_path.parent / (tmp_path.name + '-external.log')
    external.write_text('EXTERNAL-CONTENT')
    (tmp_path / 'linked.log').symlink_to(external)
    try:
        result = asyncio.run(bash(ToolContext(tmp_path),
            'false && echo unused > stale.log && echo unused > linked.log'))
        assert result.is_error
        assert 'STALE-CONTENT' not in result.output
        assert 'EXTERNAL-CONTENT' not in result.output
        assert 'No updated redirected workspace log' in result.output
    finally:
        external.unlink()


def test_failure_includes_console_error_and_bounded_log_tail(tmp_path):
    result = asyncio.run(bash(ToolContext(tmp_path),
        "sh -c 'yes diagnostic | head -c 20000; echo final-error; exit 3' > build.log"))
    assert result.is_error
    assert 'final-error' in result.output
    assert len(result.output) < 4000
    console = asyncio.run(bash(ToolContext(tmp_path), "sh -c 'echo console-error >&2; exit 4'"))
    assert 'exit code 4' in console.output
    assert 'console-error' in console.output


def test_success_preserves_stdout(tmp_path):
    result = asyncio.run(bash(ToolContext(tmp_path), 'printf generated-preview.png'))
    assert not result.is_error
    assert result.metadata['returncode'] == 0
    assert result.output == 'generated-preview.png'
