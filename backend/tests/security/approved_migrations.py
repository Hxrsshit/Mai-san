"""The approved migration list: the one place a migration is recorded.

Not a test module (no `test_` prefix). The per-stage "this stage added no
migration" pins (6J, 6K, 6L, 6M.2, 6M.4) and the exact-list test in
`test_approved_migrations.py` all compare the files on disk with this
tuple, so a new migration fails every one of them until it is added here --
deliberately, with the approving stage noted. Introduced in Stage 7B L0 so a
new migration is one reviewed edit instead of five copies of the same list.
An edit to `APPROVED_MIGRATIONS` *is* a migration approval; review it as one.

Discovery follows Alembic, not the naming convention (security review M3):
Alembic loads every `*.py` in the versions directory, and `alembic revision`
names files after a random hex id, so a `0*.py` glob would miss most of
them. `test_approved_migrations.py` also checks the revision graph Alembic
itself builds.
"""

import sys
from pathlib import Path
from typing import List, Tuple

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT_LOCATION = BACKEND / "alembic"
VERSIONS = SCRIPT_LOCATION / "versions"

APPROVED_MIGRATIONS = (
    "0001_initial_schema.py",
    "0002_memories.py",
    "0003_memory_unique_constraint.py",
    "0004_entities.py",
    "0005_relationships.py",
    "0006_knowledge_conflicts.py",
    "0007_execution.py",
    "0008_execution_conversation.py",
    "0009_workflows.py",
    "0010_history_import.py",
    "0011_reminders.py",
    "0012_tasks.py",
    "0013_task_capabilities.py",
    "0014_runner_events.py",
    "0015_approval_grants.py",
    "0016_background_runtime.py",
    "0017_monitoring.py",
    "0018_task_notifications.py",
    "0019_notification_deliveries.py",  # Stage 6M.1
    "0020_organisation_identity.py",    # Stage 7A
    "0021_knowledge_scope.py",          # Stage 7B L0
    "0022_scoped_uniqueness.py",        # Stage 7B units 5-6
)


def is_revision_file(name: str) -> bool:
    """Whether Alembic would load `name` as a revision script.

    Mirrors `alembic.script.base._only_source_rev_file` (private, so mirrored
    rather than imported; a test checks the two agree): every `*.py` except
    `__init__*` and editor lock files `.#*`. The repository sets neither
    `sourceless` nor `recursive_version_locations` (a test checks), so
    `.pyc` files and subdirectories are not loaded.
    """
    return name.endswith(".py") and not name.startswith(("__init__", ".#"))


def migration_files(versions: Path = VERSIONS) -> List[str]:
    """Every file in `versions` that Alembic loads as a migration, sorted."""
    return sorted(p.name for p in versions.iterdir() if p.is_file() and is_revision_file(p.name))


def after(prefix: str, names) -> List[str]:
    """The names that sort after the four-digit `prefix` -- including any
    file that does not start with a digit, which sorts after every number."""
    return [name for name in names if name[:4] > prefix]


def script_directory(location: Path = SCRIPT_LOCATION):
    """Alembic's own view of the scripts at `location`, configured from the
    repository's `alembic.ini`. Reads files only: no database, no `env.py`."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(location))
    saved = list(sys.path)
    try:  # from_config prepends `prepend_sys_path`; leave sys.path as it was
        return ScriptDirectory.from_config(config)
    finally:
        sys.path[:] = saved


def chain_problems(location: Path = SCRIPT_LOCATION) -> List[str]:
    """How Alembic's revision graph at `location` differs from one straight
    line through `APPROVED_MIGRATIONS`, in order. Empty when it matches."""
    expected: List[Tuple[str, str]] = [(name[:4], name) for name in APPROVED_MIGRATIONS]
    try:
        script = script_directory(location)
        heads, bases = list(script.get_heads()), list(script.get_bases())
        walk = list(reversed(list(script.walk_revisions("base", "heads"))))
    except Exception as exc:  # a cycle, a missing parent, an unloadable file
        return [f"alembic could not build the graph: {type(exc).__name__}: {exc}"]
    problems = []
    if heads != [expected[-1][0]]:
        problems.append(f"heads {heads} != [{expected[-1][0]!r}]")
    if bases != [expected[0][0]]:
        problems.append(f"bases {bases} != [{expected[0][0]!r}]")
    found = [(s.revision, Path(s.path).name) for s in walk]
    if found != expected:
        problems.append(f"walk {found} != approved {expected}")
    previous = None
    for s in walk:
        if s.down_revision != previous:
            problems.append(f"{s.revision} follows {s.down_revision!r}, not {previous!r}")
        if s.branch_labels or s.dependencies:
            problems.append(f"{s.revision} has branch labels or dependencies")
        previous = s.revision
    return problems


__all__ = [
    "APPROVED_MIGRATIONS",
    "BACKEND",
    "SCRIPT_LOCATION",
    "VERSIONS",
    "after",
    "chain_problems",
    "is_revision_file",
    "migration_files",
    "script_directory",
]
