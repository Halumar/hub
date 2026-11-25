# Security model

This page describes what protects your home when you use the Halumar Hub, who enforces which rule,
and where the limits are. To report a security problem, see [SECURITY.md](../SECURITY.md).

## Parts and trust

| Part | Runs where | Trusted with |
|---|---|---|
| Home Assistant | your system | everything; it is the source of truth for states and control |
| Halumar Hub (this app) | an app container on the same system | the Supervisor token of the app (Home Assistant API with administrator rights), its own database, the encrypted device tokens |
| Halumar integration | inside Home Assistant | creating and removing one Home Assistant user per device, checking every command of a device on **Hub only** or **Checked** |
| Halumar apps | the paired devices | their own device secret and, only when the device holds a key (see [Home Assistant access](#home-assistant-access)), the token of their own Home Assistant user |

## Admin access

- The admin page opens through Home Assistant Ingress, for Home Assistant administrators only. The
  hub trusts an Ingress request only when it comes from the Supervisor's Ingress proxy; the header
  alone is not enough.
- Direct access in the home network (`https://<home-assistant>:8100`) is off by default. It can
  only be switched on from the admin page opened through Home Assistant. While it is on,
  administrators sign in with their Home Assistant user; sessions last at most 24 hours, and
  state-changing requests need a CSRF token. Switching it off ends every direct session.
- Sign-in and pairing attempts are rate limited per network address.
- The admin API token and the integration token are stored only as hashes. The integration token
  is replaced on every start of the hub and handed to the integration through the Supervisor.

## Devices

- **One Home Assistant user per device**, without administrator rights, created and removed by the
  integration. The Home Assistant logbook therefore names the device that switched something.
  Revoking a device removes its user and token at once.
- **Pairing** needs a PIN or a code confirmed on an already paired phone or tablet. The PIN is
  short-lived; a confirmation code is valid for 5 minutes and is used up by the first scan; wrong
  codes are limited per device and per hub. A device set up by another device never gets more
  rights than the device that set it up.
- **Device type is fixed at pairing.** The platform a device reports when it pairs (iPhone, iPad,
  Apple TV, Apple Watch, Android phone, Android tablet, Android TV) decides its default profile, which rules apply
  to it, whether it counts as the watch of a phone and whether it can be a wall panel or TV. Later
  connections cannot change it: a device that reports another platform is refused and the audit
  view notes it (at most once per device every 10 minutes). The one exception is an Android phone
  and an Android tablet, which the app tells apart by the current screen size; the stored type
  stays. To change a device's type, remove it and pair it again.
- **Hub connection:** the apps reach the hub only over TLS on port 8100. The certificate is
  self-signed; its fingerprint is pinned at pairing (the admin page shows it to compare). After a
  new certificate every app has to be paired again.
- **Home Assistant connection:** a device that holds a key talks to Home Assistant directly. A
  device on **Hub only** without remote access never talks to Home Assistant; it reads and switches
  through the hub. Away from home the apps use
  only `https://` with a checked certificate. When Home Assistant is reachable at home only over
  `http://`, an app sends its Home Assistant token there only when the home network is proven (the
  pinned hub answers, or Home Assistant is found with Bonjour with the same instance ID as at
  pairing). A profile rule decides whether a person may allow unencrypted connections anyway.
- **Remote access** is a profile rule. Without it, the device's Home Assistant user is local only,
  and Home Assistant itself refuses it from outside the home network. On **Hub only**, remote access
  gives the device a read-only key (see below).
- **Secrets at rest:** device secrets are stored as hashes. Home Assistant tokens for the devices
  are encrypted with a key in a separate file. That key and the TLS private key are never part of
  Home Assistant backups.

## Home Assistant access

The profile rule **Home Assistant access** decides whether a device holds a key to its own Home
Assistant user and how it switches. It has three levels, from strict to open:

| Level | Key on the device | What the device can read | How it switches |
|---|---|---|---|
| **Hub only** | none | only what its profile shows, through the hub | through the hub, which checks the profile and then has the integration check the command again |
| **Checked** | read-only | every state in Home Assistant, also what its profile hides | only through the integration's checked action |
| **Direct** | full user rights (never administrator) | every state in Home Assistant | anything its Home Assistant user may do; the rules apply in the apps only |

- **Hub only** works where the hub is reachable, which means at home. The hub reads states, history
  and weather forecasts for the device and switches with the device's own Home Assistant user, so
  the Home Assistant logbook still names the device. The key stays on the hub; the app never
  receives it.
- **Hub only with remote access:** to work away from home the device needs a key. It then gets a
  read-only key, exactly like **Checked**: it can read every state in Home Assistant from anywhere,
  including the areas and devices its profile hides, and it can still switch only what its profile
  allows. The admin page says so wherever remote access is switched on for such a device. Widgets,
  Siri and shortcuts, controls, complications and a watch need such a key, so on **Hub only** they
  are available only with remote access.
- **TVs and watches never run without a key.** Apple TV, Android TV and Apple Watch read and
  switch through Home Assistant, so **Hub only** counts as **Checked** there (a
  read-only key); on **Direct** they hold a full key. The admin page does not offer **Hub only** for
  TV profiles. A watch exists only next to a phone that holds a key, and only a device that holds a
  key can set up a TV or a watch.
- **Cameras and media artwork:** a device without a key gets still images of the cameras in its
  profile and media artwork through the hub, refreshed while they are on screen. There is no live
  video without a key.
- **Losing the key:** when a device stops holding a key (its level changes to **Hub only** without
  remote access, remote access on **Hub only** ends, also at the end of a temporary adjustment),
  the hub has the integration replace the device's token. Widgets, Siri and shortcuts end at the
  same moment, and a watch of the device is removed. When the access of a device that holds a key
  closes, its token is replaced as well. When a guest reaches its end date, the hub removes it, and
  the integration removes its Home Assistant user and token.
- **The old key works until its token is removed.** The old key keeps working until the hub has
  received the replacement and the integration has removed the old token. While the integration
  cannot be reached this can take a while; the hub keeps trying. The device page shows "Key being
  revoked" until then. A modified app can use the old key during that time.
- **Defaults:** guests, babysitters, house sitters, children and the Kids' room panel start on
  **Hub only**. Guests and children may be set to **Checked**, never to **Direct**. Adults start on
  **Checked**; the suggestion Parents uses **Direct**.

## Who enforces which rule

Every rule in a profile is marked in the admin page as **Enforced** or **Applies in Halumar**.

| Situation | Enforced by | What that means |
|---|---|---|
| **Hub only** | the hub, then the integration | The hub serves only the areas and devices of the profile and refuses everything else. It checks the access hours and the **Access** switch, then has the integration carry out the command with the device's read-only user; the integration checks the command again like one of a **Checked** device. |
| **Checked** | the integration, inside Home Assistant | The device's Home Assistant user may only read. Every command goes through the integration's checked action, which asks which device is calling, which profile it has, whether the call comes from home or away, and what the rules say. Areas, controls, blocked devices, access hours and the **When away** rules then also hold for any other tool that uses the device's token. The integration keeps a copy of the rules and its own clock, so this works while the hub restarts. |
| **Direct** | the Halumar apps | The device's Home Assistant user has normal rights. The rules apply in the apps; someone with technical knowledge could use the device's token outside Halumar. |
| **Parental controls** | the hub and the integration | Kids rules (blocked device types, bedtime, rate limit, pause and volume limits) are checked by the hub on every action; when the device holds a key and is away from home, the integration asks the hub to check and count the action. Whether the device holds a key is set by **Home Assistant access** (**Hub only** by default). Parental controls need Halumar Pro in the app on the child's device. Without Pro the child's device shows a notice and controls nothing; the hub and the integration enforce the rules either way. |
| Guests, house sitters, babysitters | hub and integration | **Hub only** by default, at most **Checked**; access ends at the end date at the latest, and the hub then rejects the device. |
| Access hours and the **Access** switch | hub and integration | Outside the access the device sees nothing; the hub and Home Assistant refuse every command. |
| Display rules (appearance, header, card style, …) | the apps | Comfort settings, no protection. |

If the integration stops responding, devices on **Checked** and **Hub only** can still read, but
switching stops with a message and Home Assistant shows a repair. Halumar never switches a device to
**Direct** by itself. When someone gives a checked device's user more rights by hand, the
integration sets it back to read-only and logs an alarm.

## Limits

These are limits of Home Assistant or of the design, not bugs:

- **Hiding is not a read permission.** Home Assistant has no read permissions per entity. A device
  with a key (**Checked**, **Direct**, or **Hub only** with remote access) can read every state in
  Home Assistant, also of entities its profile hides. Only devices on **Hub only** without remote
  access are limited to what the hub gives them.
- **Hub only needs the hub.** A device on **Hub only** without remote access is offline while the
  hub restarts or updates, and away from home.
- **Not every Home Assistant feature is checked.** Some side features that Home Assistant does not
  route through a service call check, for example notifications and shopping lists, are not covered
  by the checked action.
- **Hub entities are visible to every Home Assistant user.** Every user can read the
  `sensor.halumar_*` entities, including the text of notifications sent to a device and device
  values such as battery or brightness. Do not send notifications whose text must stay hidden from
  other users. The pairing PIN is never in an entity attribute.
- **Pairing QR code in Home Assistant (optional).** With the option `pairing_qr_camera` the
  integration's camera shows the pairing QR code, which contains the PIN. Home Assistant gives every
  signed-in user a camera image and does not tell the camera who asks, and the camera's image
  address works without sign-in for 5 to 10 minutes (renewed every 5 minutes, the last two valid).
  So while the code is shown, everyone who can sign in to this Home Assistant, including paired
  devices that hold a key (also guest and child devices on **Checked** or with remote access), and
  anyone with a current image address can read it and use the one-time code before the intended
  person. With `guests` it is shown only during a guest pairing window, and such a PIN only adds a
  guest with the profile and end chosen when the window opened, whatever the app sends; a guest has
  no parental controls, so a child's device could add one (with children in the house keep the
  option `off`, or accept that). With `all` a member window's PIN adds a member device; without a
  profile chosen for the window it gets the default profile of the device type the pairing app
  reports. Off by default; prefer `guests` over `all`. The window lasts 5 minutes, the PIN works
  once, only administrators open a window, a live camera stream ends when a window opens or ends and
  when its viewer's access ends, the audit log notes when the QR code was shown (once per window),
  and a device paired with it is marked "paired with the code shown on the Home Assistant camera" in
  the log and its notification.
- **The hub has administrator rights in Home Assistant** through the Home Assistant API of the
  Supervisor; that is how it reads your setup and keeps its entities. Anyone who controls the hub
  container controls Home Assistant.
- **Home network trust.** "At home" is decided by network address, the way Home Assistant decides
  it for local-only users. Extra address ranges you list as home (for example a VPN) count as home
  for Halumar; Home Assistant itself does not count them for local-only users.
- **Lost or compromised devices.** A device can use what it holds (its device secret and, when it
  holds a key, its Home Assistant token). Revoke a lost or stolen device in **Devices**;
  that removes its Home Assistant user at once.
- **Not an alarm system.** Notifications depend on Home Assistant running; alerts appear in the
  apps while they are open.

## Logging

The **Log** in the admin page records activity, changes and alarms (repeated refusals, attempts to
get around a rule, an integration that stops responding). Only Home Assistant administrators see
it. PINs and tokens are never written to the log; the hub's own log masks secrets.

## Network exposure

Never forward ports 8099 or 8100 to the internet. Port 8099 is plain HTTP for Ingress and the
integration on the same system; port 8100 is meant for the home network. For access away from home,
use Home Assistant's remote access.
