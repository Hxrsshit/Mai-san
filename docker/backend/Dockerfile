# Mai backend -- FastAPI on Python 3.12
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer is cached across source changes.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

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
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
