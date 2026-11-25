"""Alias of the ``api.ws`` package (same module object)."""

import sys

from api import ws

sys.modules[__name__] = ws
