from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_install_script_has_valid_bash_syntax() -> None:
    subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / "install_local.sh")],
        check=True,
    )


def test_run_ready_script_has_valid_bash_syntax() -> None:
    subprocess.run(
        ["bash", "-n", str(ROOT / "scripts" / "run_server_ready.sh")],
        check=True,
    )
