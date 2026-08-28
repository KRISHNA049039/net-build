"""Waitress entrypoint. Serves the WSGI app in a single process, thread pool."""
import logging
import os

from waitress import serve

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

from backend.wsgi import application

log = logging.getLogger("serve")

if __name__ == "__main__":
    host = os.environ.get("SERVE_HOST", "127.0.0.1")
    port = int(os.environ.get("SERVE_PORT", "8000"))
    threads = int(os.environ.get("SERVE_THREADS", "8"))
    connection_limit = int(os.environ.get("SERVE_CONNECTION_LIMIT", "300"))

    log.info(
        "Starting waitress on %s:%s (threads=%s, connection_limit=%s)",
        host, port, threads, connection_limit,
    )
    try:
        serve(application, host=host, port=port, threads=threads,
          channel_timeout=15,      # kill idle sockets after 15s, not ~60
          cleanup_interval=5,      # sweep for dead ones every 5s
          connection_limit=connection_limit)  # headroom so a stuck state can't lock you out
    except OSError:
        log.exception("Failed to bind %s:%s", host, port)
        raise