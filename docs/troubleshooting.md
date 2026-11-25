# Troubleshooting

The texts below are quoted from the hub and its admin page. `Halumar` stands where the code
inserts the product name; `<...>` marks a value the message fills in.

Before you report a problem, set the option `log_level` to `debug`, restart the app, reproduce the
problem, and add the app log and the diagnostics of the Halumar integration to your
[issue](https://github.com/Halumar/hub/issues).

## The hub does not start

When the hub finds data it cannot safely use, it does not delete anything. It answers every request
with 503, writes the reason to the log, and creates a Home Assistant notification:

> **Halumar cannot start.** &lt;reason&gt;
>
> What to do: &lt;action&gt;

| Reason starts with | What to do |
|---|---|
| "The Halumar database &lt;path&gt; is damaged and there is no usable snapshot to restore. Nothing was changed or deleted." | Restore a Home Assistant backup of the app, or turn on `reset_corrupt_database` and restart. The hub starts with an empty database and keeps the damaged file as `hub.db.corrupt-<time>`. Turn the option off afterwards. |
| "The Halumar database &lt;path&gt; was written by a different Halumar build and does not match this version" | Restore a Home Assistant backup made with this version, or turn on `reset_corrupt_database` and restart (the old file is kept as `hub.db.mismatch-<time>`; pair the apps again). Turn the option off afterwards. |
| "The Halumar database could not be carried over to schema v&lt;n&gt;" | Go back to the previous version of the app, or turn on `reset_corrupt_database` and restart to start empty (pair the apps again and set up the profiles). |
| A damaged or missing key file | Turn on `reset_encryption_key` and restart. The hub creates a new key; the stored device tokens can no longer be read, and the integration issues new ones. Turn the option off afterwards. The key file is never part of Home Assistant backups, so a backup does not bring it back. |

After a start that restored a snapshot or replaced the key, `GET /api/health` reports
`"data_recovered": true` and the log names what happened. Turn on **Watchdog** on the app page so
the Supervisor restarts the hub when its health check fails.

## The integration is not connected

The **Setup** assistant and **Settings > System & logs** show the state of the integration:

- "No request from the integration since the hub started. In Home Assistant it is
  offered automatically (discovery); for manual installs generate an integration token under
  Advanced." Install the integration, restart Home Assistant, and add **Halumar** under
  **Settings > Devices & services**.
- "The integration seems to use an old token. Restart the hub to re-announce it, or regenerate the
  integration token (Advanced) and reconfigure the integration." The hub replaces the integration
  token on every start and announces it through the Supervisor.
- "The integration is connected but does not use live push. Update the integration and reload it
  in Home Assistant." Install the integration of the same version as the hub.
- "Home Assistant &lt;version&gt; is too old. Halumar needs Home Assistant &lt;min&gt; or later."
  The minimum is 2025.11.0.

## The admin page

- "The admin page is only available through Home Assistant." You opened port 8100 directly while
  direct access is off (the default). Open **Halumar** in the Home Assistant sidebar, or switch on
  **Settings > Advanced > Admin page > Also open the admin page directly at home** from there.
- "Too many login attempts. Please try again later." Direct sign-in is rate limited; wait a
  minute.
- "Your Home Assistant session is no longer valid. Reload the page." Reload the page in Home
  Assistant.
- "Connection to the hub lost. Retrying…" The hub restarts or is stopped; check the app log.

## Apps do not find the hub

- The apps find the hub with Bonjour (`_halumar._tcp`). Check that `mdns_enabled` is on and that
  the device is in the same network as Home Assistant. Networks that block multicast between
  clients (guest Wi-Fi, some mesh systems) prevent discovery; enter the address in the app instead.
- The apps connect on port 8100 with TLS. If **Settings > System & logs** says "TLS is off. Enable
  tls_enabled in the configuration of the hub app in Home Assistant and restart it to serve
  https/wss on port 8100.", turn the option on.
- After a restore from a backup, or after **Regenerate certificate**, the hub has a new certificate.
  The apps refuse the new fingerprint until they are paired again.

## A device cannot switch something

- Look at the device in **Devices > Effective rules**: the rule, its value and where it comes from.
- The **Log** (tab **Entries > Activity**) shows refused commands with the reason.
- On **Checked** and **Hub only**, switching needs the integration. If it does not respond, Home
  Assistant shows a repair; these devices can read but not switch until it is back.
- A child's device shows a notice that parental controls need Halumar Pro and controls nothing:
  parental controls need Halumar Pro in the app on the child's device. The hub and the integration
  enforce the rules either way.
- Outside its access hours, or with the **Access** switch off, a device sees nothing.

## A device on Hub only

The device list and the device page show each device's **Home Assistant access** (**Hub only**,
**Checked** or **Direct**) and its key (none, read-only or full).

- **A guest or another device on Hub only shows no areas.** The app is older than the hub: older
  apps wait for a Home Assistant key that a device on **Hub only** without remote access never
  gets. Install the current Halumar app on the device.
- **A device on Hub only is offline while the hub restarts or updates.** Without remote access it
  has no key and reads and switches only through the hub. It connects again by itself once the hub
  is back.
- **Widgets, Siri, controls or a watch are not available.** On **Hub only** they need the device's
  own key, which it gets only with remote access. The profile editor shows "Needs the device's own
  Home Assistant key: not possible with “Hub only” without remote access".
- **A device on Hub only cannot set up a TV or a watch.** TVs and watches never run without a key,
  and a device never hands out more than it has. Allow remote access, or set the TV up from a
  device on **Checked** or **Direct**.
- **The device page says "Key being revoked".** The device no longer gets a key, but the key its app
  knew keeps working until the hub has received the replacement and the integration has removed the
  old token. Check that the integration is connected; the hub keeps trying.

## Away from home

A device works away from home only when its profile allows remote access and Home Assistant is
reachable from outside (for example Home Assistant Cloud or your own external address in
**Settings > Network**). Remote access needs the device's own Home Assistant key: on **Hub only**
it gives the device a read-only key, and the integration checks every action. Without remote
access a device on **Hub only** works only at home. Home Assistant
does not count VPN ranges such as 100.64.0.0/10 as home for local-only users, even when you list
them under **Other addresses that count as home**.

## After restoring a backup

Every app has to be paired again: the encryption key and the TLS private key are not part of
backups by design. See [Configuration > Backups](configuration.md#backups).
