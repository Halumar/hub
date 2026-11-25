# Configuration

The hub has seven options on the **Configuration** tab of the app in Home Assistant. They are
defined in [`halumar/config.yaml`](../halumar/config.yaml) (`options` and `schema`); their names
and help texts in Home Assistant come from [`halumar/translations/`](../halumar/translations/)
(English and German). Everything else is set in the admin page (see the
[tour](admin-ui-tour.md)).

## Options

| Option | Type | Default | Meaning |
|---|---|---|---|
| `log_level` | `trace`, `debug`, `info`, `warning`, `error` | `info` | How much the hub writes to its log. `trace` logs the same as `debug`. Use `debug` when you report a problem. |
| `mdns_enabled` | bool | `true` | Announce the hub in the home network with Bonjour (`_halumar._tcp`) so the apps find it without an address. When off, enter the address in the app. |
| `tls_enabled` | bool | `true` | Serve HTTPS/WSS on port 8100 with a self-signed certificate. The apps pin its fingerprint at pairing. When off, port 8100 is not served and the apps cannot connect. Leave it on. |
| `reset_corrupt_database` | bool | `false` | Recovery only. When the hub reported a damaged database without a usable snapshot, or a database written by a different build, it starts with an empty database on the next start. The old file is kept in the app's data folder. Turn the option off again afterwards. |
| `reset_encryption_key` | bool | `false` | Recovery only. When the hub reported a damaged or missing key file, it creates a new key. The stored device tokens can no longer be read; the integration issues new ones, and a device that cannot reach Home Assistant meanwhile has to be paired again. Turn the option off again afterwards. |
| `api_docs` | bool | `true` | Serve the API documentation (`/api/docs`, `/api/redoc`, `/api/openapi.json`). Only Home Assistant administrators can open it. When off, these paths answer 404. |
| `pairing_qr_camera` | `off`, `guests`, `all` | `off` | When the integration's camera **Pairing status** (`camera.halumar_hub_pairing_qr_code`) shows the pairing QR code, for example on a dashboard. `off`: never; the image says the QR code is off. `guests`: only while a guest pairing window is open; a scanned code can only add a guest with the profile and end chosen when the window opened. `all`: during every pairing window. While a code is shown, these can read it: everyone who can sign in to this Home Assistant, every paired device that holds a key (also guest and child devices on **Checked** or with remote access), and for 5 to 10 minutes anyone with the camera's image address, without signing in. Any of them can use the one-time code before the intended guest. A child's device could use the code to add a guest device, and guest devices have no parental controls: with children in the house keep it `off`, or accept that. Prefer `guests` over `all`. See the [security model](security-model.md#limits). |

Example in the YAML mode of the Configuration tab:

```yaml
log_level: info
mdns_enabled: true
tls_enabled: true
reset_corrupt_database: false
reset_encryption_key: false
api_docs: true
pairing_qr_camera: "off"
```

## Ports

| Port | Protocol | Used by |
|---|---|---|
| 8099/tcp | HTTP | Home Assistant Ingress (the admin page in the sidebar) and the Halumar integration on the same system. Administrators cannot sign in over this port from the network. |
| 8100/tcp | HTTPS/WSS | The Halumar apps, with certificate pinning. The admin page only when direct access is switched on in **Settings > Advanced**. |

Never forward these ports to the internet. Away from home the apps use Home Assistant's own remote
access (for example Home Assistant Cloud or a VPN), and only on devices whose profile allows
remote access. A device on **Hub only** gets a read-only Home Assistant key for that; without
remote access it works only at home, through the hub.

## What the app asks from Home Assistant, and why

Each entry is a key in [`halumar/config.yaml`](../halumar/config.yaml).

| Key | Value | Why |
|---|---|---|
| `ingress`, `ingress_port`, `ingress_stream` | `true`, `8099`, `true` | The admin page opens in the Home Assistant sidebar through Ingress. Streaming keeps the live connection of the admin page open. |
| `panel_icon`, `panel_title` | `mdi:floor-plan`, `Halumar` | Icon and title of the sidebar entry. |
| `homeassistant_api` | `true` | The hub reads entities, areas and devices of Home Assistant to build your layouts, and keeps its own `sensor.halumar_*` entities in Home Assistant, which the apps read away from home. Through this API the hub acts with administrator rights in Home Assistant. |
| `hassio_api` | `true` (default role) | The hub reads information about itself and Home Assistant from the Supervisor and announces itself to the integration (discovery). No extra Supervisor role is requested. |
| `auth_api` | `true` | With direct access switched on, Home Assistant administrators sign in to the admin page on port 8100 with their Home Assistant user. |
| `discovery` | `halumar` | On every start the hub creates a new integration token and announces address, port, token and version to the Halumar integration, so you never copy a token by hand. |
| `host_network` | `true` | The hub announces itself with Bonjour in the home network, puts its network address into the pairing QR code, and accepts the integration token only from this Home Assistant system. All three need the network of the host. |
| `ports` | `8099/tcp`, `8100/tcp` | See [Ports](#ports). |
| `watchdog` | `http://[HOST]:[PORT:8099]/api/health` | The Supervisor restarts the hub when the health check fails, for example when the database is not usable. Turn on **Watchdog** on the app page. |
| `backup: hot`, `backup_pre`, `backup_post`, `backup_exclude` | see [Backups](#backups) | Home Assistant backups run while the hub is running. |
| `startup`, `init` | `application`, `false` | Started as an application; the image brings its own init system (S6 overlay). |
| AppArmor | profile in [`halumar/apparmor.txt`](../halumar/apparmor.txt) | The server process may only read its code and the system libraries, write to its data folder and `/tmp`, use the network and start no other programs. It runs as root inside its container; the profile, not the user, limits what it may do. |

## Backups

The hub is part of Home Assistant backups, also while it runs. Before a backup it writes a
consistent copy of its database; after a restore it starts from that copy.

The key that encrypts the stored device tokens and the private key of the TLS certificate are
**left out of backups on purpose**. After a restore, also on the same system, the hub creates a new
key and a new certificate: every app has to be paired again. Daily and pre-migration snapshots of
the database are left out too.

## Updating

Updates appear in Home Assistant like for any other app. Read the changelog shown on the update
dialog first: entries marked **Breaking** need your attention, for example when the hub needs the
integration of the same version. Update the hub and the
[integration](https://github.com/Halumar/integration) together. Before a database migration the hub
keeps a snapshot of the old database. Updates of the hub never change your profiles or devices; new
versions of suggestions are offered, and you decide whether to take them over.

## Uninstalling

1. Remove the Halumar integration under **Settings > Devices & services**. It removes the Home
   Assistant users it created for the paired devices, and with them their tokens.
2. Uninstall the app. Home Assistant deletes its data folder (database, keys, certificate, images).
3. Optional: remove the app repository under **Settings** > **Apps**.

The apps on the devices can then no longer connect; reset them in the app.
