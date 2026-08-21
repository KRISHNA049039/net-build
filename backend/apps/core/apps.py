import os
import sys

from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        # Guard 1: skip during management commands that shouldn't hold a connection.
        if len(sys.argv) > 1 and sys.argv[1] in {
            "check", "makemigrations", "migrate", "collectstatic", "shell",
        }:
            return
        # Guard 2: under runserver autoreload, only the reloaded child (RUN_MAIN=true) spawns.
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return
        from . import cassandra_session
        cassandra_session.start_poller()