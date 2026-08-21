"""Waitress entrypoint. Serves the WSGI app in a single process, thread pool."""
import os

from waitress import serve

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

from backend.wsgi import application

if __name__ == "__main__":
    host = os.environ.get("SERVE_HOST", "127.0.0.1")
    port = int(os.environ.get("SERVE_PORT", "8000"))
    serve(application, host=host, port=port, threads=8,
      channel_timeout=15,      # kill idle sockets after 15s, not ~60
      cleanup_interval=5,      # sweep for dead ones every 5s
      connection_limit=300)    # headroom so a stuck state can't lock you out