"""ASGI entry point: Django, with Socket.IO mounted at ``/socket.io/``.

``daphne`` (first in INSTALLED_APPS) makes ``manage.py runserver`` serve this
application, so development gets WebSockets with the usual command. Production
runs the same object under any ASGI server, e.g. ``daphne config.asgi:application``.
"""
import os

import socketio
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_application = get_asgi_application()

# Imported after Django is set up: the server reads settings and the ORM.
from apps.core.realtime import sio  # noqa: E402

application = socketio.ASGIApp(sio, other_asgi_app=django_application, socketio_path="socket.io")
