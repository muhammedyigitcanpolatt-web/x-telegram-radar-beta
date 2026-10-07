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


@pytest.mark.skipif(os.name == "nt", reason="The gateway bootstrap uses a POSIX shell")
@pytest.mark.parametrize(
    ("route", "expected_success"),
    [
        ("default via 172.23.0.1 dev eth0", True),
        ("default via 172.23.0.999 dev eth0", False),
        ("172.23.0.0/16 dev eth0", False),
        ("default via 172.23.0.1 dev eth0\ndefault via 172.24.0.1 dev eth1", False),
    ],
)
def test_gateway_bootstrap_trusts_only_valid_default_gateway(tmp_path, route, expected_success):
    root = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ip = fake_bin / "ip"
    ip.write_text(f"#!/bin/sh\nprintf '%s\\n' '{route}'\n")
    ip.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["RADAR_ALLOWED_ORIGINS"] = "http://localhost:8080"
    result = subprocess.run(
        [
            "sh", "-c",
            '. "$1"; case $- in *u*) exit 90;; esac; printf "%s|%s" "$RADAR_HOST_GATEWAY_IP" "$RADAR_PUBLIC_HTTPS"',
            "sh", str(root / "gateway.envsh"),
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if expected_success:
        assert result.returncode == 0
        assert result.stdout == "172.23.0.1|0"
    else:
        assert result.returncode != 0
        assert "172.23.0.999" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="The gateway bootstrap uses a POSIX shell")
@pytest.mark.parametrize(
    ("origins", "expected_mode"),
    [
        ("http://localhost:8080,http://127.0.0.1:8080", "0"),
        ("https://radar.example.com", "1"),
        ("https://radar.example.com, https://alt.example.com", "1"),
        ("https://radar.example.com,http://localhost:8080", None),
        ("http://radar.example.com,https://radar.example.com", None),
        ("HTTPS://radar.example.com", None),
        ("", None),
    ],
)
def test_gateway_bootstrap_origin_mode(tmp_path, origins, expected_mode):
    root = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ip = fake_bin / "ip"
    ip.write_text("#!/bin/sh\nprintf '%s\\n' 'default via 172.23.0.1 dev eth0'\n")
    ip.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["RADAR_ALLOWED_ORIGINS"] = origins
    env["RADAR_EDGE_SHARED_SECRET"] = "0123456789abcdef" * 4
    result = subprocess.run(
        ["sh", "-c", '. "$1"; printf "%s" "$RADAR_PUBLIC_HTTPS"', "sh", str(root / "gateway.envsh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if expected_mode is None:
        assert result.returncode != 0
    else:
        assert result.returncode == 0
        assert result.stdout == expected_mode


@pytest.mark.skipif(os.name == "nt", reason="The gateway bootstrap uses a POSIX shell")
@pytest.mark.parametrize(
    ("secret", "accepted"),
    [
        ("0123456789abcdef" * 4, True),
        ("", False),
        ("short", False),
        ("A" * 64, False),
        ("0" * 64, False),
    ],
)
def test_public_gateway_requires_valid_edge_secret(tmp_path, secret, accepted):
    root = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ip = fake_bin / "ip"
    ip.write_text("#!/bin/sh\nprintf '%s\\n' 'default via 172.23.0.1 dev eth0'\n")
    ip.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["RADAR_ALLOWED_ORIGINS"] = "https://radar.example.com"
    env["RADAR_EDGE_SHARED_SECRET"] = secret
    result = subprocess.run(
        ["sh", "-c", '. "$1"; printf "%s" "$RADAR_PUBLIC_HTTPS"', "sh", str(root / "gateway.envsh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if accepted:
        assert result.returncode == 0
        assert result.stdout == "1"
    else:
        assert result.returncode != 0
