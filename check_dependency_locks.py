"""Fail CI if the test lock could replace a pinned production dependency."""

from pathlib import Path
import re


PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]\n]+\])?==([^\s\\]+)", re.MULTILINE)


def pins(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, version in PIN.findall(Path(path).read_text(encoding="utf-8")):
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in result and result[normalized] != version:
            raise SystemExit(f"Conflicting pins for {normalized} in {path}")
        result[normalized] = version
    if not result:
        raise SystemExit(f"No pinned packages found in {path}")
    return result


runtime = pins("requirements.txt")
development = pins("requirements-dev.lock")
missing = sorted(runtime.keys() - development.keys())
mismatched = sorted(
    name for name, version in runtime.items() if development.get(name) != version
)
if missing or mismatched:
    raise SystemExit(
        f"Development lock differs from production: missing={missing}, mismatched={mismatched}. "
        "Regenerate requirements-dev.lock with requirements.txt when changing runtime pins."
    )
print(f"Dependency locks agree on {len(runtime)} production pins.")
