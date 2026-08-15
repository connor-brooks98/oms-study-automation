from __future__ import annotations

import hashlib
import json
import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
LOCK = ROOT / "scripts" / "a0-rehearsal-windows-py312.lock.json"
VERIFIER = ROOT / "scripts" / "verify-a0-rehearsal-runtime-lock.py"


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _write_distribution(
    root: Path, directory: str, *, name: str | None, version: str | None
) -> None:
    metadata = root / f"{directory}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    fields = ["Metadata-Version: 2.1"]
    if name is not None:
        fields.append(f"Name: {name}")
    if version is not None:
        fields.append(f"Version: {version}")
    metadata.write_text("\n".join(fields) + "\n", encoding="utf-8")


def _runtime_lock(distributions: list[dict[str, str]]) -> dict[str, object]:
    return {
        "cache_tag": sys.implementation.cache_tag,
        "distribution_count": len(distributions),
        "distributions": distributions,
        "distributions_sha256": hashlib.sha256(_canonical_json(distributions)).hexdigest(),
        "implementation": sys.implementation.name,
        "python_version": ".".join(str(value) for value in sys.version_info[:3]),
        "schema_version": 1,
    }


def test_tracked_windows_runtime_lock_is_canonical_and_complete() -> None:
    document = json.loads(LOCK.read_text(encoding="utf-8"))
    assert set(document) == {
        "cache_tag",
        "distribution_count",
        "distributions",
        "distributions_sha256",
        "implementation",
        "python_version",
        "schema_version",
    }
    assert document["schema_version"] == 1
    assert document["python_version"] == "3.12.10"
    assert document["implementation"] == "cpython"
    assert document["cache_tag"] == "cpython-312"
    assert document["distribution_count"] == len(document["distributions"]) == 79
    assert hashlib.sha256(_canonical_json(document["distributions"])).hexdigest() == document[
        "distributions_sha256"
    ]
    assert document["distributions_sha256"] == (
        "efe8c28015f62650e6f9f549066c98773ade4ee4d2039a7b547b147be0fd0318"
    )
    locked = {item["name"].casefold(): item["version"] for item in document["distributions"]}
    assert locked["fastapi"] == "0.141.1"
    assert locked["sqlalchemy"] == "2.0.52"
    assert locked["starlette"] == "1.6.0"
    assert locked["uvicorn"] == "0.52.3"
    assert "oms-study-automation" not in locked


def test_runtime_lock_verifier_requires_exact_distribution_closure(tmp_path: Path) -> None:
    verifier = runpy.run_path(str(VERIFIER), run_name="runtime_lock_test")
    installed = [
        {"name": "fastapi", "version": "0.141.1"},
        {"name": "uvicorn", "version": "0.52.3"},
    ]
    distributions_sha256 = hashlib.sha256(_canonical_json(installed)).hexdigest()
    lock = {
        "cache_tag": "cpython-312",
        "distribution_count": 2,
        "distributions": installed,
        "distributions_sha256": distributions_sha256,
        "implementation": "cpython",
        "python_version": "3.12.10",
        "schema_version": 1,
    }
    verifier["_verify_runtime_lock"](
        lock,
        installed,
        python_version="3.12.10",
        implementation="cpython",
        cache_tag="cpython-312",
    )
    with pytest.raises(RuntimeError, match="distribution closure"):
        verifier["_verify_runtime_lock"](
            lock,
            [*installed, {"name": "starlette", "version": "1.6.0"}],
            python_version="3.12.10",
            implementation="cpython",
            cache_tag="cpython-312",
        )


def test_runtime_lock_verifier_main_scans_synthetic_metadata_and_excludes_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verifier = runpy.run_path(str(VERIFIER), run_name="runtime_lock_test")
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    _write_distribution(dependency, "zeta-2.0", name="Zeta", version="2.0")
    _write_distribution(dependency, "alpha_pkg-1.0", name="Alpha_Pkg", version="1.0")
    _write_distribution(
        dependency,
        "oms_study_automation-9.9",
        name="oms-study-automation",
        version="9.9",
    )
    installed = [
        {"name": "Alpha_Pkg", "version": "1.0"},
        {"name": "Zeta", "version": "2.0"},
    ]
    lock = tmp_path / "runtime.lock.json"
    lock.write_text(json.dumps(_runtime_lock(installed)), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VERIFIER),
            "--lock",
            str(lock),
            "--dependency-path",
            str(dependency),
        ],
    )

    assert verifier["main"]() == 0

    expected_digest = hashlib.sha256(_canonical_json(installed)).hexdigest()
    assert capsys.readouterr().out == (
        f"RUNTIME_LOCK_OK count=2 sha256={expected_digest}\n"
    )


def test_runtime_lock_verifier_rejects_duplicate_normalized_installed_names(
    tmp_path: Path,
) -> None:
    verifier = runpy.run_path(str(VERIFIER), run_name="runtime_lock_test")
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    _write_distribution(dependency, "demo_one-1.0", name="Demo.Pkg", version="1.0")
    _write_distribution(dependency, "demo_two-2.0", name="demo-pkg", version="2.0")

    with pytest.raises(RuntimeError, match="names are not unique"):
        verifier["_installed_distributions"]([dependency])


@pytest.mark.parametrize(
    ("name", "version"),
    ((None, "1.0"), ("Incomplete", None)),
)
def test_runtime_lock_verifier_rejects_incomplete_installed_metadata(
    tmp_path: Path, name: str | None, version: str | None
) -> None:
    verifier = runpy.run_path(str(VERIFIER), run_name="runtime_lock_test")
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    _write_distribution(dependency, "incomplete-1.0", name=name, version=version)

    with pytest.raises(RuntimeError, match="metadata is incomplete"):
        verifier["_installed_distributions"]([dependency])


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"schema_version": 2}, "runtime lock is malformed"),
        ({"distribution_count": 3}, "distribution count is invalid"),
        ({"distributions_sha256": "0" * 64}, "distribution digest is invalid"),
        ({"python_version": "0.0.0"}, "interpreter identity does not match"),
    ),
)
def test_runtime_lock_verifier_rejects_tampered_lock_identity(
    mutation: dict[str, object], message: str
) -> None:
    verifier = runpy.run_path(str(VERIFIER), run_name="runtime_lock_test")
    installed = [{"name": "FastAPI", "version": "0.141.1"}]
    lock = _runtime_lock(installed) | mutation

    with pytest.raises(RuntimeError, match=message):
        verifier["_verify_runtime_lock"](
            lock,
            installed,
            python_version=".".join(str(value) for value in sys.version_info[:3]),
            implementation=sys.implementation.name,
            cache_tag=sys.implementation.cache_tag,
        )


def test_runtime_lock_verifier_rejects_relative_dependency_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = runpy.run_path(str(VERIFIER), run_name="runtime_lock_test")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(VERIFIER), "--dependency-path", "relative-site-packages"],
    )

    with pytest.raises(RuntimeError, match="unavailable or indirect"):
        verifier["main"]()
