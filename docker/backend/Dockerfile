# Mai backend -- FastAPI on Python 3.12
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer is cached across source changes.
#
# `constraints.txt` pins every transitive version as well as the direct ones,
# so the image that ships is the image that was audited. Without it, a rebuild
# months later silently resolves different versions of two dozen packages.
#
# Test tooling is deliberately absent: `requirements-dev.txt` is not installed
# here, so pytest and its dependencies are not part of the runtime surface.
COPY backend/requirements.txt ./requirements.txt
COPY backend/constraints.txt ./constraints.txt
# pip itself is upgraded first. The base image ships a pip old enough to carry
# seven advisories of its own; it is a build tool rather than a runtime
# import, but it is installed in the image and an auditor cannot tell the
# difference from the outside.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -c constraints.txt -r requirements.txt

COPY backend/ ./

# Run as a non-root user.
RUN useradd --create-home --uid 1000 mai && chown -R mai:mai /app

# The OAuth credential directory, created in the image so the named volume
# inherits this owner and mode when Docker initialises it. Without this the
# volume is created root-owned 0755 and the unprivileged process cannot write
# a token -- a failure that only appears at the end of the consent flow, after
# the user has already granted access at Google.
RUN mkdir -p /var/lib/mai/credentials \
    && chown -R mai:mai /var/lib/mai \
    && chmod 700 /var/lib/mai/credentials

USER mai

EXPOSE 8000

# Migrations run first so the schema is never created implicitly by app code.
# `exec` replaces the shell with uvicorn so it runs as PID 1 and receives the
# SIGTERM from `docker stop` directly. Without it, `sh` holds PID 1, does not
# forward the signal, and the container is SIGKILLed after the grace period --
# the lifespan shutdown branch never runs.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
