"""Family manager (contract ``docs/family.md``).

A member device with the effective profile rule ``family_manager`` manages
the child devices of its scope (device property): kids rules as device
adjustments of the child devices (never what the child's profile fixes),
short releases, the children's requests ("Can I have more time?") and a read view
of their activity. The hub decides everything; apps only show.

* :mod:`data` – ``data/family.json`` (byte copy of the contract)
* :mod:`check` – permission check, port of ``tools/family_ref.py``
* :mod:`store` – the family tables of migration 11 (scopes, releases,
  requests, tokens, events)
* :mod:`service` – the operations (WS, integration relay, admin REST)
* :mod:`notify` – Home Assistant notifications with answer buttons
* :mod:`events` – activity of child devices and the event hook
* :mod:`expiry` – planner: releases and requests end on time
"""
