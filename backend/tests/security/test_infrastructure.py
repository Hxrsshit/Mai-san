"""Stage 3D: static security review of Docker, compose and the frontend.

**Static review only.** Neither Docker nor the frontend can be executed on this
machine, so these tests assert properties of the *configuration*. They do not
and cannot establish runtime behaviour, which stays NOT VERIFIED.

They are still worth executing: every finding here was a real configuration
defect, and a test is what stops it coming back.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMPOSE = REPO / "docker-compose.yml"
DOCKERIGNORE = REPO / ".dockerignore"
BACKEND_DOCKERFILE = REPO / "docker" / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = REPO / "docker" / "frontend" / "Dockerfile"
FRONTEND = REPO / "frontend"

requires_compose = pytest.mark.skipif(
    not COMPOSE.exists(), reason="no docker-compose.yml"
)


def compose_text() -> str:
    return COMPOSE.read_text()


# --- Finding I-01: the live API key would be baked into the image -----------
# `COPY backend/ ./` copies the entire directory, including backend/.env, which
# holds the live provider key. An image layer is permanent: deleting the file
# later does not remove it, and `docker history` or a registry push exposes it.


def test_a_dockerignore_exists() -> None:
    assert DOCKERIGNORE.exists(), (
        "without .dockerignore the whole build context is copied into the image"
    )


@pytest.mark.parametrize(
    "pattern", [".env", "*.db", ".venv/", "node_modules/", ".git/"]
)
def test_the_build_context_excludes_secrets_and_local_data(pattern) -> None:
    lines = {
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert pattern in lines, f".dockerignore does not exclude {pattern}"


def test_the_env_file_would_not_reach_an_image_layer() -> None:
    """The specific regression: backend/.env inside the build context."""
    ignored = {
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in ignored
    assert "**/.env" in ignored, "a nested backend/.env would still be copied"


def test_no_dockerfile_copies_an_env_file_explicitly() -> None:
    for dockerfile in (BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE):
        if not dockerfile.exists():
            continue
        for line in dockerfile.read_text().splitlines():
            stripped = line.strip()
            if stripped.upper().startswith(("COPY", "ADD")):
                assert ".env" not in stripped or ".env.example" in stripped, (
                    f"{dockerfile.name}: {stripped}"
                )


def test_no_secret_is_passed_as_a_build_argument() -> None:
    """ARG values are recorded in image metadata and readable afterwards."""
    for dockerfile in (BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE):
        if not dockerfile.exists():
            continue
        for line in dockerfile.read_text().splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("ARG"):
                name = stripped[3:].split("=")[0].strip().upper()
                assert not name.endswith(
                    ("API_KEY", "SECRET", "PASSWORD", "TOKEN", "PRIVATE_KEY")
                ), f"{dockerfile.name} takes {name} as a build arg"


# --- Finding I-02: services published on every interface --------------------
# Docker binds 0.0.0.0 unless told otherwise. The API has no authentication and
# exposes DELETE routes; the database holds the entire personal knowledge base.


@requires_compose
def test_every_published_port_binds_to_loopback() -> None:
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"\s*$', compose_text(), re.MULTILINE)
    assert published, "no published ports found -- has the format changed?"

    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), (
            f'port mapping "{mapping}" binds every interface; '
            f"prefix it with 127.0.0.1"
        )


@requires_compose
def test_the_database_password_has_no_default() -> None:
    """`${VAR:-default}` ships a guessable credential; `${VAR:?msg}` refuses."""
    text = compose_text()
    assert "POSTGRES_PASSWORD:-" not in text, (
        "the database password falls back to a default value"
    )
    assert "POSTGRES_PASSWORD:?" in text, (
        "the database password should be required, not defaulted"
    )


def test_the_example_env_ships_no_working_database_password() -> None:
    example = (REPO / ".env.example").read_text()
    for line in example.splitlines():
        if line.strip().startswith("POSTGRES_PASSWORD="):
            _, _, value = line.partition("=")
            assert not value.strip(), "the example ships a usable password"


@requires_compose
def test_no_secret_is_hardcoded_in_compose() -> None:
    """Every credential must come from the environment, not the file."""
    for number, line in enumerate(compose_text().splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip().upper().endswith(
            ("API_KEY", "PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY")
        ):
            assert "${" in value, f"docker-compose.yml:{number} hardcodes {key.strip()}"


# --- Container hardening ----------------------------------------------------


@pytest.mark.parametrize(
    "dockerfile", [BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE], ids=["backend", "frontend"]
)
def test_containers_drop_root(dockerfile) -> None:
    if not dockerfile.exists():
        pytest.skip(f"{dockerfile} not present")
    directives = [
        line.strip()
        for line in dockerfile.read_text().splitlines()
        if line.strip().upper().startswith("USER")
    ]
    assert directives, f"{dockerfile.name} never switches away from root"
    assert not directives[-1].split()[1].startswith(("root", "0")), (
        f"{dockerfile.name} runs as root"
    )


# --- Frontend static review -------------------------------------------------


def frontend_sources():
    if not FRONTEND.exists():
        return []
    return [
        path
        for path in FRONTEND.rglob("*")
        if path.suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs"}
        and "node_modules" not in path.parts
        and ".next" not in path.parts
    ]


requires_frontend = pytest.mark.skipif(
    not FRONTEND.exists(), reason="no frontend directory"
)


@requires_frontend
def test_no_secret_is_exposed_through_a_public_env_var() -> None:
    """`NEXT_PUBLIC_*` is inlined into the client bundle and is world-readable."""
    offenders = []
    pattern = re.compile(r"NEXT_PUBLIC_[A-Z0-9_]+")

    for path in list(frontend_sources()) + [
        p for p in REPO.rglob(".env.example") if "node_modules" not in p.parts
    ] + [COMPOSE, FRONTEND_DOCKERFILE]:
        if not path.exists():
            continue
        for name in pattern.findall(path.read_text(errors="ignore")):
            if name.endswith(("KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL")):
                offenders.append(f"{path.name}:{name}")

    assert offenders == [], f"secrets in client-visible variables: {offenders}"


@requires_frontend
def test_no_raw_html_injection_sink_is_used() -> None:
    """`dangerouslySetInnerHTML` renders model and memory text as markup."""
    offenders = [
        str(path.relative_to(REPO))
        for path in frontend_sources()
        if "dangerouslySetInnerHTML" in path.read_text(errors="ignore")
    ]
    assert offenders == [], f"raw HTML sinks: {offenders}"


@requires_frontend
def test_no_other_html_injection_sink_is_used() -> None:
    sinks = ("innerHTML", "outerHTML", "document.write", "eval(", "new Function(")
    offenders = []
    for path in frontend_sources():
        text = path.read_text(errors="ignore")
        for sink in sinks:
            if sink in text:
                offenders.append(f"{path.name}:{sink}")
    assert offenders == [], f"injection sinks: {offenders}"


@requires_frontend
def test_no_personal_data_is_persisted_in_browser_storage() -> None:
    """Browser storage outlives the session and is readable by any script."""
    offenders = []
    for path in frontend_sources():
        text = path.read_text(errors="ignore")
        for api in ("localStorage", "sessionStorage", "document.cookie"):
            if api in text:
                offenders.append(f"{path.name}:{api}")
    assert offenders == [], f"browser storage use: {offenders}"


@requires_frontend
def test_the_api_url_is_not_hardcoded_to_a_remote_host() -> None:
    """A stale remote default would send private conversations off-machine."""
    pattern = re.compile(r"https?://(?!localhost|127\.0\.0\.1)[a-z0-9.-]+", re.I)
    offenders = []
    for path in frontend_sources():
        for match in pattern.findall(path.read_text(errors="ignore")):
            if "w3.org" in match or "schema.org" in match:
                continue
            offenders.append(f"{path.name}:{match}")
    assert offenders == [], f"non-local URLs in the frontend: {offenders}"
