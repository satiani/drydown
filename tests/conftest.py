"""Test bootstrap.

`appdaemon` is not installed in CI/local dev, so we inject a minimal stub
into ``sys.modules`` before ``drydown`` is imported. The stub's ``Hass`` base
class mirrors the small slice of the AppDaemon API the app actually uses
(``log``, ``get_state``, ``set_state``, scheduler methods); each test wires
the bits it needs.
"""

from __future__ import annotations

import os
import sys
import types

# Make the repo root importable so `from apps.drydown import drydown` works
# without the app needing to be an installed package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _install_appdaemon_stub() -> None:
    if "appdaemon" in sys.modules:
        return

    ad = types.ModuleType("appdaemon")
    pkg_hass = types.ModuleType("appdaemon.plugins")
    pkg_hass_hass = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        """Minimal stand-in for appdaemon.plugins.hass.hassapi.Hass."""

        def __init__(self, *args, **kwargs):
            self.args = {}
            self._set_states = {}

        # Logging ---------------------------------------------------------
        def log(self, msg, *args, level="INFO", **kwargs):
            # AppDaemon uses lazy % formatting; emulate it for readability.
            try:
                formatted = msg % args if args else msg
            except Exception:
                formatted = f"{msg} {args}"
            print(f"[{level}] {formatted}")

        # State -----------------------------------------------------------
        def get_state(self, entity_id, attribute=None, default=None, **kwargs):
            # Tests override this; default behaviour returns None.
            return default

        def set_state(self, entity_id, state=None, attributes=None, **kwargs):
            self._set_states[entity_id] = {"state": state, "attributes": attributes or {}}
            return state

        # Scheduler -------------------------------------------------------
        def run_in(self, callback, delay, **kwargs):
            return None

        def run_hourly(self, callback, start=None, **kwargs):
            return None

    hassapi.Hass = Hass

    ad.plugins = pkg_hass
    pkg_hass.hass = pkg_hass_hass
    pkg_hass_hass.hassapi = hassapi

    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = pkg_hass
    sys.modules["appdaemon.plugins.hass"] = pkg_hass_hass
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi


_install_appdaemon_stub()
