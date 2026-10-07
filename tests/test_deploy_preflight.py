import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="The deploy script uses a POSIX shell")
def test_deploy_check_rejects_example_secret_before_running_docker(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / ".env"
    env_file.write_text("POSTGRES_PASSWORD=REPLACE_WITH_RANDOM_HEX\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "docker-called"
    docker = fake_bin / "docker"
    docker.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    docker.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["RADAR_ENV_FILE"] = str(env_file)
    result = subprocess.run(
        ["bash", str(root / "deploy.sh"), "check"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "example values" in result.stderr
    assert not marker.exists()
