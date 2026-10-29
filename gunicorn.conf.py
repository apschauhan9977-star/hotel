# Gunicorn configuration for production
# Docs: https://docs.gunicorn.org/en/stable/settings.html

import multiprocessing
import os

# (2 x CPU cores) + 1 — standard recommendation for sync workers
workers = multiprocessing.cpu_count() * 2 + 1

# Bind to all interfaces on port 8000 (Nginx/reverse proxy sits in front)
bind = "0.0.0.0:8000"

# Use 'sync' for SQLite (thread-safe enough at this scale).
# Switch to 'uvicorn.workers.UvicornWorker' if you migrate to async / websockets.
worker_class = "sync"

# Graceful timeout for worker shutdown
timeout = 30

# Restart workers periodically to prevent memory leaks
max_requests = 1000
max_requests_jitter = 50

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Preload app once before forking workers (saves memory + speeds boot)
preload_app = True
