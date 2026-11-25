# Documentation of the Halumar Hub

## For users

| Topic | Where |
|---|---|
| Installation and first steps | [README](../README.md#installation), and the first part of the user guide |
| User guide: profiles, setting up devices, rules, remote access, guests, access hours, parental controls and family managers, wall panels, the log | [halumar/DOCS.md](../halumar/DOCS.md) (English and German; Home Assistant shows it on the app's **Documentation** tab) |
| Configuration: options, ports, permissions, backups, updating, uninstalling | [configuration.md](configuration.md) |
| A short tour of the admin page | [admin-ui-tour.md](admin-ui-tour.md) |
| Security model and its limits | [security-model.md](security-model.md) |
| Troubleshooting with the hub's own messages | [troubleshooting.md](troubleshooting.md) |
| Changes per version | [halumar/CHANGELOG.md](../halumar/CHANGELOG.md) (shown in Home Assistant and used for the GitHub release notes) |

### Concepts in one paragraph each

- **Device:** a paired iPhone, iPad, Apple TV, Apple Watch, Android phone or tablet (also as a
  wall panel) or Android TV. Each has its own Home Assistant user, created by the integration. A watch is set up
  through its phone and has no hub connection of its own.
- **Profile:** every device has exactly one. It holds a value for every rule; rules can be fixed
  for all devices of the profile or adjusted for one device, also for a limited time. See
  [Profiles](../halumar/DOCS.md#profiles).
- **Rules:** which areas a device shows, how much it may control, whether it works away from home,
  which features and which look it gets. The rule catalog says on which device types each rule
  applies. See [Rules that hold](../halumar/DOCS.md#rules-that-hold).
- **Home Assistant access:** a profile rule with three levels. **Hub only**: no Home Assistant
  key, reading and switching only through the hub. **Checked**: a read-only key, switching through
  the integration's checked action. **Direct**: full user rights. See
  [Rules that hold](../halumar/DOCS.md#rules-that-hold) and the
  [security model](security-model.md#home-assistant-access).
- **Guests:** devices with an end date, at most 90 days per grant, on **Hub only** by default and
  never on **Direct**. See [Guests](../halumar/DOCS.md#guests).
- **Access hours:** week windows in which a device has access, plus an **Access** switch in Home
  Assistant for automations. See
  [Access hours, house sitter and babysitter](../halumar/DOCS.md#access-hours-house-sitter-and-babysitter).
- **Family:** parental controls (kids rules enforced by the hub and the integration, **Hub only**
  by default, Halumar Pro in the app on the child's device) and family managers. See
  [Parental controls and family managers](../halumar/DOCS.md#parental-controls-and-family-managers).
- **Remote access:** a profile rule; away from home a device uses Home Assistant's own remote
  access. On **Hub only** it gives the device a read-only key. See [When
  away](../halumar/DOCS.md#when-away).

## For developers

| Topic | Where |
|---|---|
| Building, testing, the checks CI runs, commit messages | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Architecture: components, authentication, data model, CI/CD | [halumar/ARCHITECTURE.md](../halumar/ARCHITECTURE.md) |
| Developer reference: authentication, WebSocket flow, pairing, sensors, building clients | [DEVELOPER.md](DEVELOPER.md) |
| API: public contract, internal parts, how to get the OpenAPI document | [api.md](api.md) |
| Releases and versions | [release-versioning.md](release-versioning.md) |
| Code layout of the backend services | [halumar/rootfs/opt/hub/services/README.md](../halumar/rootfs/opt/hub/services/README.md) |
