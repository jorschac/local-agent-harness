from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_build_and_wrapper_have_valid_shell_syntax():
    for script in [ROOT / 'harness', ROOT / 'scripts/bootstrap.sh']:
        result = subprocess.run(['bash', '-n', str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_install_is_locked_noneditable_and_wrapper_needs_no_activation():
    wrapper = (ROOT / 'harness').read_text()
    script = (ROOT / 'scripts/bootstrap.sh').read_text()
    assert '.venv/bin/harness' in wrapper
    assert '--locked' in script and '--link-mode copy' in script
    assert '--no-editable' in script and '--reinstall-package local-agent-harness' in script
    assert 'PYTHONPATH=' not in script and 'source .venv/bin/activate' not in wrapper
