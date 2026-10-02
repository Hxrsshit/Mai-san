"""`app.database.metadata` is the complete registry, on its own.

Found in Stage 6M.1 live verification: the registry did not import the
workflows models, so a standalone process that imported it but not `app.main`
failed at flush with `NoReferencedTableError` on `executions.workflow_id`.
The suite never saw it, because `conftest.py` imports `app.main`, whose routes
import the workflows models. So the check runs in a fresh subprocess.
"""

import json

_REGISTRY_ONLY = r"""
import json, sys
from app.database.metadata import Base

# Proves the check is not vacuous: nothing else registered the tables.
assert "app.main" not in sys.modules, "the registry imported app.main"

# Raises NoReferencedTableError for a foreign key to an unregistered table.
for table in Base.metadata.sorted_tables:
    for fk in table.foreign_keys:
        fk.column
registry = sorted(Base.metadata.tables)

import app.main  # noqa: F401  (registers whatever the server would)
print(json.dumps({"registry": registry, "server": sorted(Base.metadata.tables)}))
"""


def test_the_registry_alone_resolves_every_foreign_key() -> None:
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", _REGISTRY_ONLY], cwd=backend, capture_output=True,
        text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(backend), "LOG_LEVEL": "WARNING"},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert {"workflows", "executions"} <= set(outcome["registry"])
    # Complete: the server process registers no table the registry missed.
    assert outcome["registry"] == outcome["server"]
