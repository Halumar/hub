"""Technical namespace of the hub (level B of the naming model).

Every identifier that Home Assistant, the integration or the apps see and that
depends on the frozen namespace is built here and nowhere else
(contracts/namespace.json). The display brand lives in ``brand.py``; code
symbols are named after their function.
"""

from typing import Final

NAMESPACE: Final = "halumar"

# Supervisor discovery service = HA integration domain (service calls
# ``<domain>.ack_notifications``, events ``<domain>_*``).
HA_DOMAIN: Final = NAMESPACE
DISCOVERY_SERVICE: Final = NAMESPACE

# Bonjour/mDNS
BONJOUR_SERVICE_TYPE: Final = f"_{NAMESPACE}._tcp.local."

# Pairing deep link / QR content
DEEP_LINK_SCHEME: Final = NAMESPACE
DEEP_LINK_PAIR: Final = f"{DEEP_LINK_SCHEME}://pair"

# Entities mirrored into HA through the states API
ENTITY_PREFIX: Final = f"{NAMESPACE}_"
CONFIG_SENSOR_ID: Final = f"sensor.{ENTITY_PREFIX}config"
GUESTS_SENSOR_ID: Final = f"sensor.{ENTITY_PREFIX}guests"
AREA_SENSOR_PREFIX: Final = f"sensor.{ENTITY_PREFIX}area_"
AREA_SWITCH_PREFIX: Final = f"input_boolean.{ENTITY_PREFIX}area_"
CLIENT_SENSOR_PREFIX: Final = f"sensor.{ENTITY_PREFIX}client_"
AREA_SWITCH_ID_ATTRIBUTE: Final = f"{ENTITY_PREFIX}area_id"

# HA persistent notification about data safety (damaged database / key file)
HA_NOTIFICATION_ID_DATA_SAFETY: Final = f"{ENTITY_PREFIX}data_safety"

# Family manager: answer buttons of HA notifications. The action id is
# ``<NS>_FAMILY_<token>``; the tag replaces/clears the notification per request.
FAMILY_ACTION_PREFIX: Final = f"{NAMESPACE.upper()}_FAMILY_"
FAMILY_NOTIFICATION_TAG_PREFIX: Final = f"{NAMESPACE}-family-"

# HA device registry identifiers reported to the integration
HA_DEVICE_IDENTIFIER_HUB: Final = f"{ENTITY_PREFIX}hub"
HA_DEVICE_IDENTIFIER_CLIENT_PREFIX: Final = f"{ENTITY_PREFIX}client_"


def area_sensor_id(slug: str) -> str:
    return f"{AREA_SENSOR_PREFIX}{slug}"


def area_switch_id(slug: str) -> str:
    return f"{AREA_SWITCH_PREFIX}{slug}"


def client_notification_sensor_id(slug: str, uid8: str) -> str:
    return f"{CLIENT_SENSOR_PREFIX}{slug}_{uid8}_notifications"


def client_quick_access_sensor_id(slug: str, uid8: str) -> str:
    return f"{CLIENT_SENSOR_PREFIX}{slug}_{uid8}_quick_access"


def ha_device_identifier_client(client_id: str) -> str:
    return f"{HA_DEVICE_IDENTIFIER_CLIENT_PREFIX}{client_id}"
