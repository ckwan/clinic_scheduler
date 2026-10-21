# One image for the API, the Temporal worker, and the migration job (the command differs).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so code changes do not reinstall them.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y clinic_scheduler

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY app ./app

RUN useradd --create-home --uid 10001 clinic_scheduler && chown -R clinic_scheduler /app
USER clinic_scheduler

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
