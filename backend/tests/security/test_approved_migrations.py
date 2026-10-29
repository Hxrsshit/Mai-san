"""Stage 7B L0: the migration directory is exactly the approved list.

Stricter than the per-stage pins it backs: the whole list, in order, not only
the tail -- and the revision chain must be one straight line, both as the
files declare it and as Alembic itself builds it (security review M3).
"""

import re
import shutil

import pytest

from tests.security.approved_migrations import (
    APPROVED_MIGRATIONS,
    BACKEND,
    VERSIONS,
    after,
    chain_problems,
    is_revision_file,
    migration_files,
)


def test_the_migration_files_are_exactly_the_approved_list() -> None:
    assert migration_files() == list(APPROVED_MIGRATIONS)


def test_the_approved_migrations_form_one_linear_chain() -> None:
    previous = None
    for name in APPROVED_MIGRATIONS:
        source = (VERSIONS / name).read_text(encoding="utf-8")
        revision = re.search(r'^revision: str = "([^"]+)"', source, flags=re.M).group(1)
        down = re.search(r'^down_revision: Union\[str, None\] = (None|"([^"]+)")', source, flags=re.M)
        assert revision == name[:4], name
        assert (down.group(2) if down.group(2) else None) == previous, name
        previous = revision
    assert previous == APPROVED_MIGRATIONS[-1][:4]


def test_alembics_own_revision_graph_is_the_approved_line() -> None:
    """One head (the last approved revision), one base, and a walk from base
    to head that visits exactly the approved files in order."""
    assert chain_problems() == []


# --- discovery follows Alembic, not the naming convention ---------------------

def test_discovery_agrees_with_alembics_own_file_rule() -> None:
    from alembic.script.base import _only_source_rev_file   # private; pinned here

    names = ["0001_x.py", "c3a9f1e2d4b5_rogue.py", "x.py", "_x.py", ".hidden.py",
             "__init__.py", "__init__x.py", ".#0001_x.py", "0001_x.pyc", "notes.txt"]
    for name in names:
        assert is_revision_file(name) == bool(_only_source_rev_file.match(name)), name


def test_alembic_reads_only_the_versions_directory_and_only_source() -> None:
    """What `is_revision_file` mirrors assumes the default loading: no
    `.pyc`-only scripts, no subdirectories, no other version locations."""
    ini = (BACKEND / "alembic.ini").read_text(encoding="utf-8")
    active = [line.split("=")[0].strip() for line in ini.splitlines()
              if line.strip() and not line.lstrip().startswith(("#", ";"))]
    for option in ("sourceless", "recursive_version_locations", "version_locations"):
        assert option not in active, option


# --- regressions: each would have passed before review M3 ----------------------

ROGUE = '''"""An unapproved migration with Alembic's default hex file name."""
revision = "c3a9f1e2d4b5"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
'''


@pytest.fixture
def scripts(tmp_path):
    """A copy of the script location: the approved versions only."""
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    for name in APPROVED_MIGRATIONS:
        shutil.copy(VERSIONS / name, versions / name)
    assert chain_problems(tmp_path / "alembic") == []      # the copy itself is clean
    return tmp_path / "alembic", versions


def _rewrite(path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source, (path.name, old)
    path.write_text(source.replace(old, new), encoding="utf-8")


def test_a_migration_without_a_zero_prefix_is_detected(scripts) -> None:
    location, versions = scripts
    (versions / "c3a9f1e2d4b5_rogue.py").write_text(ROGUE, encoding="utf-8")
    # The pre-M3 glob would not have seen it ...
    assert sorted(p.name for p in versions.glob("0*.py")) == list(APPROVED_MIGRATIONS)
    # ... but Alembic loads it, and so does discovery, so every check fails:
    found = migration_files(versions)
    assert "c3a9f1e2d4b5_rogue.py" in found and found != list(APPROVED_MIGRATIONS)
    assert after("0018", found) != after("0018", APPROVED_MIGRATIONS)   # 6J/6K/6L pins
    assert after("0019", found) != after("0019", APPROVED_MIGRATIONS)   # 6M.2/6M.4 pins
    problems = chain_problems(location)
    assert any("heads ['c3a9f1e2d4b5']" in p for p in problems), problems


def test_reordered_revisions_are_detected(scripts) -> None:
    location, versions = scripts
    _rewrite(versions / "0021_knowledge_scope.py",
             'down_revision: Union[str, None] = "0020"', 'down_revision: Union[str, None] = "0019"')
    _rewrite(versions / "0020_organisation_identity.py",
             'down_revision: Union[str, None] = "0019"', 'down_revision: Union[str, None] = "0021"')
    assert migration_files(versions) == list(APPROVED_MIGRATIONS)       # names alone pass
    problems = chain_problems(location)
    assert any(p.startswith("heads ['0020']") for p in problems), problems
    assert any(p.startswith("walk ") for p in problems), problems


def test_an_unexpected_revision_id_is_detected(scripts) -> None:
    location, versions = scripts
    _rewrite(versions / "0021_knowledge_scope.py",
             'revision: str = "0021"', 'revision: str = "abc123"')
    problems = chain_problems(location)
    assert any("heads ['abc123']" in p for p in problems), problems


def test_a_branch_is_detected(scripts) -> None:
    location, versions = scripts
    (versions / "0022_branch.py").write_text(
        ROGUE.replace('"c3a9f1e2d4b5"', '"0022"').replace('"0021"', '"0020"'), encoding="utf-8")
    problems = chain_problems(location)
    assert any(p.startswith("heads ") and "'0021'" in p and "'0022'" in p for p in problems), problems


def test_an_init_module_is_not_a_migration(scripts) -> None:
    location, versions = scripts
    (versions / "__init__.py").write_text("", encoding="utf-8")
    assert migration_files(versions) == list(APPROVED_MIGRATIONS)
    assert chain_problems(location) == []
