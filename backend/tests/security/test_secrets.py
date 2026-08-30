"""Stage 3D: secret hygiene, executed as tests rather than asserted in prose.

These run against the real repository and the real Git history, so a secret
committed later fails the suite rather than waiting for the next manual audit.

Nothing here prints a secret. Assertions report the file and the pattern that
matched; the matched text itself is never echoed.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"

#: Credential shapes worth failing a build over. Each is specific enough that a
#: match is almost certainly a real key rather than prose about one.
SECRET_PATTERNS = {
    "groq_key": r"gsk_[A-Za-z0-9]{40,}",
    "github_pat_classic": r"ghp_[A-Za-z0-9]{36,}",
    "github_pat_fine": r"github_pat_[A-Za-z0-9_]{50,}",
    "openrouter_key": r"sk-or-v1-[a-f0-9]{40,}",
    "openai_key": r"sk-[A-Za-z0-9]{40,}",
    "aws_access_key": r"AKIA[A-Z0-9]{16}",
    "private_key_block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "slack_token": r"xox[baprs]-[A-Za-z0-9-]{20,}",
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    ).stdout


def tracked_files():
    return [line for line in git("ls-files").splitlines() if line.strip()]


def in_git_repo() -> bool:
    return (REPO / ".git").exists()


requires_git = pytest.mark.skipif(
    not in_git_repo(), reason="not a git repository"
)


# --- Tracked files ----------------------------------------------------------


@requires_git
@pytest.mark.parametrize("name,pattern", sorted(SECRET_PATTERNS.items()))
def test_no_secret_shape_appears_in_a_tracked_file(name, pattern) -> None:
    compiled = re.compile(pattern)
    offenders = []

    for relative in tracked_files():
        path = REPO / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if compiled.search(text):
            # Report the location only. The match itself is never echoed.
            offenders.append(relative)

    assert not offenders, f"{name} pattern found in tracked files: {offenders}"


@requires_git
def test_no_env_file_is_tracked() -> None:
    """`.env.example` is fine; anything else named `.env*` is not."""
    tracked = [
        path
        for path in tracked_files()
        if Path(path).name.startswith(".env")
        and Path(path).name != ".env.example"
    ]
    assert tracked == [], f"environment files are tracked: {tracked}"


@requires_git
def test_the_real_env_file_is_ignored() -> None:
    """The file holding the live key must be invisible to Git."""
    env = BACKEND / ".env"
    if not env.exists():
        pytest.skip("no backend/.env on this machine")

    result = subprocess.run(
        ["git", "check-ignore", "-q", str(env)],
        cwd=REPO, capture_output=True, check=False,
    )
    assert result.returncode == 0, "backend/.env is NOT ignored by git"


def test_example_env_files_carry_no_value_for_a_secret() -> None:
    """Placeholders only. A key name may appear; a key value may not."""
    # Suffix match, not substring: "LLM_MAX_TOKENS" contains "TOKEN" and is a
    # plain integer setting. Only keys that *end* in a credential word count.
    secret_suffixes = (
        "API_KEY", "SECRET", "PASSWORD", "PRIVATE_KEY",
        "ACCESS_TOKEN", "AUTH_TOKEN", "_TOKEN",
    )

    for example in REPO.rglob(".env.example"):
        if "node_modules" in example.parts or ".venv" in example.parts:
            continue
        for number, line in enumerate(example.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            name = key.strip().upper()
            if name.endswith(secret_suffixes):
                assert not value.strip(), (
                    f"{example.name}:{number} assigns a value to {key.strip()}"
                )


# --- Git history ------------------------------------------------------------


@requires_git
@pytest.mark.parametrize("name,pattern", sorted(SECRET_PATTERNS.items()))
def test_no_secret_shape_appears_anywhere_in_history(name, pattern) -> None:
    """Deleting a secret from HEAD does not remove it from the repository.

    `git log -S` walks every reachable commit's diffs, so a key added and later
    removed is still found -- which is the case that actually matters, because
    a pushed history is permanently readable.
    """
    output = git("log", "--all", "--oneline", "-S", pattern, "--pickaxe-regex")
    commits = [line.split()[0] for line in output.splitlines() if line.strip()]
    assert commits == [], (
        f"{name} pattern appears in history at commits {commits}; "
        f"removing the file is not enough -- the key must be revoked"
    )


@requires_git
def test_no_env_file_was_ever_committed() -> None:
    """A file ignored today may have been committed yesterday."""
    output = git("log", "--all", "--name-only", "--pretty=format:")
    offenders = sorted(
        {
            path
            for path in output.splitlines()
            if path.strip()
            and Path(path).name.startswith(".env")
            and Path(path).name != ".env.example"
        }
    )
    assert offenders == [], f"environment files exist in history: {offenders}"


# --- Test fixtures ----------------------------------------------------------


def test_no_test_file_carries_a_real_credential_shape() -> None:
    """Fixtures must use obvious fakes, never a copied working key."""
    compiled = {name: re.compile(p) for name, p in SECRET_PATTERNS.items()}
    offenders = []

    for path in (BACKEND / "tests").rglob("*.py"):
        text = path.read_text(errors="ignore")
        for name, pattern in compiled.items():
            if pattern.search(text):
                offenders.append(f"{path.name}:{name}")

    assert offenders == [], f"credential shapes in test files: {offenders}"
