# Changelog

Changes of the Halumar Hub app, newest version on top. Changes that need your attention before
updating are marked **Breaking**. Versions are calendar versions (`YYYY.M.PATCH`, for example
2026.12.0); the hub and the integration of the same version belong together.

## 2026.12.0

Released 2026-11-26.

First public version of the Halumar Hub. Requires Home Assistant 2025.11 or later (Home
Assistant OS, or another installation with the Supervisor that can run apps) and the Halumar
integration of the same version, installed from the Marketplace (Home Assistant 2026.11 and
newer) or HACS (older versions).

### What it does

- Admin page in the Home Assistant sidebar: areas, sections, entity tiles and widgets (clock,
  text, spacer) on a grid, with a preview per device type and room climate per area.
- Devices: iPhone, iPad, Apple TV, Apple Watch, Android phone and tablet (also as a wall panel)
  and Android TV. Pairing with a PIN, a QR code or a setup link. A paired phone or tablet
  can set up other devices when its profile allows it; an iPhone sets up its own Apple Watch.
- Optional: show the pairing QR code in the integration's camera Pairing status (option
  `pairing_qr_camera`, off by default).
- One Home Assistant user per device, without admin rights, created and removed by the Halumar
  integration.
- Profiles with a value for every rule: visible areas, how much a device may control, Home Assistant
  access (Hub only, Checked or Direct), remote access and what a device may do when away, features
  and appearance. Rules can be fixed or adjusted for a single device, also for a limited time. Five
  profiles from the start and further suggestions to copy; updates never change your profiles.
- Guests with an end date (at most 90 days per grant), invitations by permitted members, access
  hours and an Access switch in Home Assistant, profiles for house sitters and babysitters.
- Home Assistant access per device: **Hub only** (without remote access no Home Assistant key; the
  device reads, switches and gets camera still images only through the hub and sees only what its
  profile allows), **Checked** (a read-only key; switching through the integration's checked action)
  or **Direct** (full user rights). Guests, house sitters, babysitters, children and the Kids'
  room panel start on Hub only; adults on Checked, the Parents suggestion on Direct. The device
  list and the device page show each device's level and key.
- Parental controls that an admin switches on per profile and the hub and the integration enforce
  (blocked device types, bedtime, rate limit, pause, volume limit), and family managers who answer
  the children's requests. Parental controls need Halumar Pro in the app on the child's device.
- A child's Apple Watch can be allowed per profile; it follows the iPhone's parental controls.
- Wall panels and TVs: values and controls in Home Assistant, waking from sensors, brightness
  that follows a sensor or a schedule.
- Notifications to devices, a log of activity, changes and alarms, and notifications on events.
- Discovery with Bonjour and the Supervisor; TLS with certificate pinning on port 8100.
- REST and WebSocket API described in the contract (OpenAPI and AsyncAPI); Swagger UI and ReDoc
  for administrators.
- English and German user interface and documentation; AppArmor profile; prebuilt images for
  `amd64` and `aarch64`.
- Free software under the AGPL-3.0-only; the admin page links to the source code.

### Security

- The admin page opens through Home Assistant and only for Home Assistant administrators. Direct
  access in the home network is off by default and can only be switched on from Home Assistant.
- Device secrets and tokens are stored as hashes; the Home Assistant tokens of the devices are
  encrypted with a key that is never part of backups.
- Rate limits on sign-in, pairing and setup codes.
- The pairing QR code appears in Home Assistant (the integration's camera, for example on a
  dashboard) only with the option `pairing_qr_camera`: off by default, `guests` for guest pairing
  only, `all` for every pairing. The audit log notes when it was shown.
- A device on Hub only holds no Home Assistant key without remote access. Remote access gives it a
  read-only key, with which it can read every state in Home Assistant; the admin page says so
  wherever remote access is switched on for such a device. When a device stops holding a key, the
  hub has its token replaced. The old key keeps working until the hub has received the replacement
  and the integration has removed the old token. While the integration cannot be reached this can
  take a while; the hub keeps trying.
- What the rules can and cannot prevent is described in the
  [security model](https://github.com/Halumar/hub/blob/main/docs/security-model.md).
