import os
import subprocess
import sys
from pathlib import Path

import pytest

from oms_hub.study_generation.service import (
    GenerationPrerequisiteError,
    GenerationService,
)


def test_application_constructs_without_legacy_sdk(tmp_path):
    """A fresh interpreter cannot hide the SDK behind an already imported module."""
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import importlib.abc
import sys
from pathlib import Path

class NoLegacySDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "notebooklm" or fullname.startswith("notebooklm."):
            raise ModuleNotFoundError("Legacy NotebookLM SDK is unavailable")

sys.meta_path.insert(0, NoLegacySDK())
from oms_hub.app import create_app
from oms_hub.config import Settings

root = Path(sys.argv[1])
app = create_app(Settings(
    _env_file=None, data_dir=root, database_url=f"sqlite:///{root / 'hub.db'}",
    study_root=root / "study", study_backend="codex_subscription",
))
try:
    assert app.state.notebook_auth is None
    assert app.state.notebook_connection.status().state == "retired"
    assert app.state.generation_service.backend == "codex_subscription"
    assert app.state.studio_worker.import_worker is not None
    assert not any(name == "notebooklm" or name.startswith("notebooklm.")
                   for name in sys.modules)
finally:
    app.state.database.close()
"""
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("OMS_HUB_")
    }
    environment["PYTHONPATH"] = str(source)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, env=environment, cwd=tmp_path, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["outline", "quiz"])
def test_retired_generation_rejects_before_any_dependency_or_account_access(kind):
    service = GenerationService(object(), object(), object(), object(), object())

    with pytest.raises(GenerationPrerequisiteError, match="retired"):
        getattr(service, f"queue_{kind}")(4)
