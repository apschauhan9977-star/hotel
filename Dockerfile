# ---- Build stage ----
FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# The SQLite DB file lives here; create a volume mount point
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Point the app at the persistent volume for the DB
ENV HOTELPRO_DB_PATH=/app/data/hotelpro.db

# Expose the Gunicorn port
EXPOSE 8000

# Run with Gunicorn (production WSGI server). 4 worker processes is a sane
# default for a small/medium deployment; tune to (2 x CPU cores) + 1.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", \
     "--access-logfile", "-", "--error-logfile", "-", "app:app"]
