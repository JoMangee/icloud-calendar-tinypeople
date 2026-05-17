# cPanel Passenger entrypoint.
# LiteSpeed calls `application` as the WSGI callable.
# Startup file:  passenger_wsgi.py
# Entry point:   application

from icloud_bridge import app as application  # noqa: F401
