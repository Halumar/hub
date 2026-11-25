"""Curated Material Design Icon list for the notification icon picker.

Exposed as a module constant so the frontend picker can fetch it via
``GET /api/notifications/icons`` and the backend can validate/suggest it.

These are *suggestions* only — the notification create API still accepts any
``mdi:*`` name typed manually.
"""

# Flat, curated list of common MDI names (with the ``mdi:`` prefix), grouped
# loosely by theme via comments. ~55 entries.
CURATED_NOTIFICATION_ICONS: list[str] = [
    # Alerts / status
    "mdi:bell",
    "mdi:bell-ring",
    "mdi:bell-alert",
    "mdi:bell-badge",
    "mdi:alert",
    "mdi:alert-circle",
    "mdi:alert-octagon",
    "mdi:information",
    "mdi:information-outline",
    "mdi:check-circle",
    "mdi:close-circle",
    "mdi:help-circle",
    "mdi:shield-alert",
    "mdi:shield-check",
    # Home / rooms
    "mdi:home",
    "mdi:home-alert",
    "mdi:door",
    "mdi:door-open",
    "mdi:garage",
    "mdi:window-open",
    "mdi:stairs",
    # Lighting / power
    "mdi:lightbulb",
    "mdi:lightbulb-on",
    "mdi:lamp",
    "mdi:power-plug",
    "mdi:flash",
    "mdi:battery",
    "mdi:battery-low",
    "mdi:battery-charging",
    # Security / access
    "mdi:lock",
    "mdi:lock-open",
    "mdi:key",
    "mdi:cctv",
    "mdi:motion-sensor",
    "mdi:account",
    "mdi:account-alert",
    # Climate / environment
    "mdi:thermometer",
    "mdi:water",
    "mdi:water-alert",
    "mdi:water-percent",
    "mdi:fire",
    "mdi:smoke-detector",
    "mdi:snowflake",
    "mdi:fan",
    "mdi:air-conditioner",
    # Weather
    "mdi:weather-sunny",
    "mdi:weather-night",
    "mdi:weather-cloudy",
    "mdi:weather-rainy",
    "mdi:weather-pouring",
    "mdi:weather-snowy",
    "mdi:weather-lightning",
    "mdi:weather-windy",
    # Connectivity / time / comms
    "mdi:wifi",
    "mdi:wifi-off",
    "mdi:message",
    "mdi:message-alert",
    "mdi:email",
    "mdi:phone",
    "mdi:calendar-clock",
    "mdi:clock-alert",
    "mdi:bell-ring-outline",
]
