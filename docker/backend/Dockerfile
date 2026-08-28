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
USER mai

EXPOSE 8000

# Migrations run first so the schema is never created implicitly by app code.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
