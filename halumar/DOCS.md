# Halumar Hub

English first, Deutsch weiter unten.

Halumar is an independent project. It is not affiliated with or endorsed by Home Assistant or the Open Home Foundation.

---

## English

The Halumar Hub gives every device in your home its own, limited view of Home Assistant: a wall panel, a guest's phone, a child's tablet, a TV or a watch each see and control only what you allow. It keeps your Halumar areas, layouts, paired devices, profiles and notifications and serves them to the Halumar apps on iPhone, iPad, Apple TV, Apple Watch, Android phone and tablet (also as a wall panel) and Android TV. The Halumar apps for iPhone, iPad, Apple TV and Apple Watch are in beta; install them through TestFlight: <https://testflight.apple.com/join/fHX7YT8v>. The apps for Android phone and tablet (wall panel) and Android TV are not available yet. You manage everything in the **Halumar** panel in the Home Assistant sidebar.

### Installation

1. Add the repository of the hub: under **Settings** > **Apps**, add `https://github.com/Halumar/hub` as an app (add-on) repository. Or use this link:

   [![Add the repository to your Home Assistant.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FHalumar%2Fhub)

2. Install **Halumar**, turn on **Start on boot**, **Watchdog** and **Show in sidebar**, and start it. On the first start the hub creates its database, a key for the device tokens, a TLS certificate for port 8100 and five profiles; it changes nothing in Home Assistant until you pair a device.
3. Install the **Halumar integration** from the Marketplace (Home Assistant 2026.11 and newer) or HACS (older versions). It is not in the catalog: add it as a custom repository with [this link](https://my.home-assistant.io/redirect/hacs_repository/?owner=Halumar&repository=integration&category=integration) or the address `https://github.com/Halumar/integration`, then install it. The Marketplace asks you to connect GitHub the first time you add a repository from a link. If Home Assistant asks for a restart, restart it. Home Assistant then shows the Halumar integration as discovered under **Settings** > **Devices & services**; select **Add**. The integration is required: it gives every paired device its own Home Assistant user.
4. Open **Halumar** in the sidebar. The **Setup** assistant checks the integration and the addresses of Home Assistant and helps you create your first area. Then add entities to your areas.
5. Pair a device: in the panel go to **Devices** > **Pair device**, keep **Member**, enter the **Owner** if the device belongs to one person, check the **Profile** (**Default per device type** is preselected) and select **Generate PIN**. Open the Halumar app on the device; it finds the hub in your network (Bonjour) and asks for the PIN shown in the panel. A phone or tablet can scan the QR code instead; on a TV you enter the PIN with the remote.
6. Optional, set up from another device: a new phone, tablet or TV shows a QR code and a 6-digit code instead of the PIN. A paired phone or tablet (iPhone, iPad, Android phone or tablet) whose profile allows **Set up new devices** scans the code or types it in, chooses what the new device is and confirms. See **Set up devices**. **Pairing with the PIN always works.**
7. Optional, guests: invite a guest yourself under **Guests** > **Invite guest**, or let a member device whose profile allows **Set up new devices** with the kind **Guest** show **Invite guest** in the app. See **Guests**.
8. Optional, babysitter or house sitter: give the device **Access hours** and use its **Access** switch in Home Assistant. See **Access hours, house sitter and babysitter**.

### Profiles

Every paired device has exactly one **profile**. A profile holds a value for each rule: which areas the device shows, how much it may control, whether it works away from home, which features it gets (widgets, Siri, watch, wall panel) and how the app looks. Change a profile and every device with it updates within seconds. You manage profiles on the **Profiles** page (up to 50).

**Suggestions.** Five profiles exist from the first start: **Adult**, **Child**, **Guest**, **Wall panel** and **TV**. Under **Suggestions** there are more: Easy mode, Toddler (up to 5), Child 6–9, Child 10–12, Teen (13–17), Babysitter, House sitter & helper, Kids' room panel and Parents. **Add** copies a suggestion into a new profile that is yours from then on. Updates of the hub never change your profiles or devices. If a suggestion gets a new version, its card shows **New version**; **What's new?** lists the changes with a preview, and you decide whether to **Take over**.

**Kind.** Every profile has a **Kind**: Person, Child, Guest, Wall panel, TV, House sitter or Babysitter. The kind changes no rule. It decides under which choice the profile appears when a new device is set up, and which rules apply at all (**Set up new devices** only for kind Person, **Answer children's requests** only for kind Babysitter). Child does not depend on parental controls: a Teen profile without parental controls is still of kind Child. Guest, House sitter and Babysitter devices get guest access with an end date.

**Fixed or adjustable per device.** Next to almost every rule there is a **Fixed** checkbox. A fixed rule is the same on every device with this profile and shows a lock on the device page and in the app. Any other rule can be adjusted for a single device. Making a rule fixed removes its adjustments; the preview lists them. Display and wall panel rules can be set to **User's choice**: the person on the device decides, the profile only gives the starting value. Each rule is marked **Enforced** (Home Assistant or the hub checks it) or **Applies in Halumar** (the app follows it).

**Adjust a single device.** Open **Devices** and select the device; **Effective rules** shows every rule and where its value comes from. Select **Adjust** next to a rule, pick the value and how long it applies: **1 hour**, **Today**, **Until tomorrow morning** (07:00, time zone of Home Assistant), **7 days**, a **Date** or **Permanent**. A temporary adjustment ends by itself; the device switches back even while it is offline. **Use profile value** removes one adjustment, **Reset everything to the profile** all of them. Loosening a security rule (for example remote access) asks once more; you can add a note.

**Halumar Pro.** Some rules need Halumar Pro in the Halumar app on that device; the profile editor marks them **Pro**: **Family manager**, **Allow a watch**, **Widgets, controls & Siri**, **Watch**, **NFC and QR tags**, **Wall panel mode**, **Control from Home Assistant**, **Wake from sensors**, **Brightness**, **Announcements and chime**, **Screen off**, and **Parental controls** with all kids rules. The app on the device checks it; the hub serves every rule to every device and has no paid features. **Home Assistant access** never needs Pro.

**Default profiles.** On the **Profiles** page under **Default profiles** choose one profile per device type (iPhone, iPad, Apple TV, Apple Watch, Android phone, Android tablet, Android TV, guests) and the default guest duration (1 day). They apply wherever nobody picks a profile: preselected when pairing and when a phone sets up a device, when a member invites a guest, for a phone's own watch, and when pairing starts from the Halumar integration.

**Create, assign, delete.** **New profile**: **From a suggestion**, **From a device** (takes over its values and adjustments) or **Empty**. Assign a profile on the device page or for several devices at once, with or without their adjustments. Every save shows a preview first (**Preview & save**): which devices change, which adjustments disappear, where a Home Assistant key is added or removed. Deleting a profile needs a replacement profile for its devices; the last profile and default profiles cannot be deleted. **History** on the device page lists every change with author and time.

### Set up devices

A new device does not ask what it is for. It shows a QR code and a 6-digit code (or takes the PIN from the panel). A phone or tablet that is already paired (iPhone, iPad, Android phone or tablet) decides the rest: in the app **Settings** > **Set up device**, scan the code or enter it, choose what the new device is, and confirm.

- **Right.** The profile rule **Set up new devices** allows it, for the kinds you choose: Guest, Child, Wall panel, TV, House sitter, Babysitter, Person. It exists only for profiles of kind Person and only on phones and tablets. Without it a device sets up nothing and invites nobody. The suggestion **Parents** allows all kinds; all other suggestions have it off. Like any rule, you can fix it or adjust it for one device.
- **Questions per kind.** Child: name, profile, areas and, for a family manager, who looks after the child. Wall panel: place, profile, start area, areas. TV: room. Guest, house sitter, babysitter: name, profile, duration (at most 90 days), remote access and access hours, as far as the profile does not fix them. Person: name and profile.
- **Never more rights.** The app offers only profiles that give the new device no more than the setting-up device has itself: no stronger **Home Assistant access**, no remote access it does not have, no wider **When away** level, no areas it does not see. You as the admin are not bound by this.
- **Overview.** In the app, **Set up by me** lists the devices and guests a member set up; it can remove them there. In the panel every device shows **Set up by**, and **Devices** lists **Set-ups in progress** with who is setting them up; you can end one.
- **Limits.** A code is valid for 5 minutes and is used up by the first scan. After 5 wrong codes per device or 20 on the hub within 10 minutes, entering codes pauses. The QR code and the code hold no secret.

**Watch.** An iPhone sets up its own Apple Watch. Open Halumar on the watch, tap **Pair** and confirm on the iPhone. The iPhone's profile needs the rule **Watch** and **Set up new devices** turned on (any kind, or none: then it can set up only its own watch). The watch becomes a device of its own with the default profile for Apple Watch. It has no hub connection of its own. Its Home Assistant access reaches it through the iPhone, encrypted for the watch, so the iPhone cannot read it. Remove the watch in the iPhone app with **Remove Apple Watch** or on its device page in the panel. When its iPhone is removed (by you, or by the member that set the iPhone up), the watch goes with it: without its iPhone it gets no access any more. The log says which iPhone it went with. Guests cannot add a watch. A device with parental controls can, when its profile has the rule **Allow a watch**.

### Rules that hold

Each profile has the rule **Home Assistant access**, from strict to open:

- **Hub only**: without remote access the device gets no Home Assistant key. It reads and switches only through the hub and sees only what its profile allows; the hub checks every action and has the integration carry it out with the device's own read-only Home Assistant user, so the logbook names the device. It works where the hub is reachable, which means at home, and it is offline while the hub restarts. With remote access the device gets a read-only key to work away from home: with it, it can read every state in Home Assistant, also what its profile hides, and still switch only what the profile allows. The admin page says so wherever you switch remote access on. Widgets, Siri, controls and a watch need such a key, so on **Hub only** they work only with remote access. Camera images reach a device without a key as still images through the hub; there is no live video without a key.
- **Checked**: the device's Home Assistant user may only read. The app, widgets, Siri and the watch switch through the checked action of the Halumar integration, which asks four questions for every command: which device is calling, which profile it has, whether the call comes from home or from away (the same way Home Assistant decides it for `local_only` users), and what the rule says. Only then does the integration switch, and the Home Assistant logbook names the device. Areas, controls, blocked devices, access hours and the **When away** rules therefore also hold for any other tool that uses the device's key.
- **Direct**: the device's Home Assistant user has normal rights and switches directly. The rules apply in the Halumar apps.

Guest, House sitter & helper, Babysitter, the children's profiles and Kids' room panel start on **Hub only**; guests and children can be set to **Checked**, never to **Direct**. Adult, Wall panel and TV use **Checked**, Parents use **Direct**. A TV or a watch is never on **Hub only**: such a device always gets at least a read-only key, and the admin page shows that. Apple TV, Android TV and watches never run without a key, so **Hub only** counts as **Checked** there; TV profiles do not offer it. For the same reason a device on **Hub only** sets up a TV or a watch only when it has remote access. The device list and the device page show each device's level and its key (none, read-only or full). Like any rule, **Home Assistant access** can be fixed or adjusted per device. When a device stops holding a key, the hub has the integration replace its token. The old key keeps working until the hub has received the replacement and the integration has removed the old token. While the integration cannot be reached this can take a while; the hub keeps trying. The device page shows **Key being revoked** until then.

The integration keeps a copy of each device's rules, so switching keeps working while the hub restarts; only kids rules that count actions (rate limit, pause) ask the hub. If the Halumar integration stops responding, devices on **Checked** and **Hub only** still show everything, but switching stops with a clear message, and Home Assistant shows a repair. Halumar never switches a device to **Direct** on its own. In an emergency you can give the device's user more rights in the user settings of Home Assistant; once the integration is back, it sets checked devices to read-only again. It does the same whenever someone changes such a user by hand.

Hiding is a display rule. Home Assistant has no read permissions per entity, so a device with a key (**Checked**, **Direct**, or **Hub only** with remote access) can read the state of entities its profile hides. Only a device on **Hub only** without remote access is limited to what the hub gives it. A few side features that Home Assistant does not check, such as notifications and shopping lists, are not covered by the check.

### When away

**Remote access** is a profile rule: on in Adult and Parents, off in Guest, House sitter & helper, Babysitter, Wall panel and TV. It needs a route to Home Assistant that you set up (for example Home Assistant Cloud or a VPN). Without remote access the device's Home Assistant user is local only, so Home Assistant itself accepts it only from your home network.

The rule group **When away** decides what a device may do when it is not at home, per kind of device (locks, garage doors, lights …) or for a single device: **Operate**, **Show only** or **Hide**, each with its own **Fixed** checkbox. Important alerts (alarm, smoke, water) are never hidden, at most shown only. On **Hub only** and **Checked**, the hub and Home Assistant enforce these rules when switching; with **Direct** they apply in the Halumar apps.

**Other addresses that count as home.** Under **Settings** > **Network** you list further address ranges that count as home, for example of a VPN; the list is empty by default. **Tailscale** adds 100.64.0.0/10 (and, if you like, its IPv6 range); **WireGuard** and **ZeroTier** open the input with an example; you can also enter your own range with a name. Only private ranges are accepted, at most 10. The apps, the hub, the checked action and the **When away** rules all treat these addresses as home, and a pairing QR code may name a hub on such an address. Mobile carriers use 100.64.x.x for their customers too (CGNAT): add Tailscale only if you use it. Every change is in the log.

### Guests

Guest access is a property of the device, chosen when pairing (**Member** or **Guest**); it cannot be changed later (pair again instead). Devices of kind House sitter and Babysitter are set up with guest access as well. A guest always has an end date: 1, 3, 7, 14 or 30 days or a date, at most 90 days per grant, extendable on the **Guests** page (**+1 day**), with **Extend** on the device page or with the integration action **Extend guest access**. Guests never invite others, set up devices, open the app settings, or use widgets, Siri, a watch, wall panel mode or location, and they use **Hub only** or **Checked**, never **Direct**. Everything else comes from the profile, remote access too: the Guest and House sitter & helper profiles have it off, and you can adjust it for a single guest. On **Hub only**, remote access gives the guest's device a read-only key (see **Rules that hold**). When the access ends, all security rules fall to their strictest value and the hub rejects the device. Guests can use any profile without parental controls.

**Invitations by members.** A member device whose profile allows **Set up new devices** with the kind **Guest** shows **Invite guest** in the app: a large QR code with a PIN, valid for 5 minutes and once. The member chooses the guest profile, the duration (at most 90 days), remote access and access hours, as far as the profile does not fix them; it never gives more than it has. The guest scans the code with the phone camera or types the PIN in the Halumar app. Open invitations are listed on the **Guests** page under **Open invitations**, where you can withdraw them. Only you change a guest afterwards; the member can remove it under **Set up by me**.

### Access hours, house sitter and babysitter

Some devices should have access only at certain times: a babysitter in the evening, a house sitter while you are away. Two controls decide, and both must allow access:

- **Access hours**, a profile rule with week windows like bedtime (for example every day 17:00–01:00). It has **Fixed**, can be adjusted per device, also for a while, and can be chosen when setting up or inviting, as far as the profile does not fix it. Times follow the time zone of Home Assistant.
- The **Access** switch in Home Assistant (`switch.halumar_<name>_access`) for your own schedules and automations, for example from a calendar or when the alarm is armed. Every device with access hours or of kind Guest, House sitter or Babysitter has one; it is on by default. Only Home Assistant administrators and automations operate it; other users and the checked action are refused. The device page in the panel shows **Access now** with the same switch (**Access allowed**).

Outside its access the device stays paired but sees nothing: no areas, no states, no control. It shows when access opens again (“Access again from Fri 17:00”), and it opens by itself. On **Hub only** and **Checked**, the hub and Home Assistant refuse every command outside the access as well; the integration checks the windows with its own clock, also while the hub is offline. Every change appears in the **Log**; the event **Access on/off** can notify you.

**House sitter & helper** (suggestion): **Hub only**, remote access off, selected areas (living room, kitchen, garden and entrance are preselected by name), every day 07:00–21:00; with remote access switched on, door, garage, gate, lock and alarm are only shown when away.

**Babysitter** (suggestion, kind Babysitter): only at home, selected areas (living spaces and kids' rooms preselected), every day 17:00–01:00. With the rule **Answer children's requests** the babysitter gets the children's requests while it has access and is at home, next to the family managers; the first answer counts. It may only answer **Yes, 15 min**, **Yes, 30 min**, **Until the end of babysitting** or **No**. It changes no rules, never allows a blocked device type, a lock or a fixed rule, and never beyond the end of its access. It sees no earlier activity, no log and no rules. Parents see every answer with its author and can end the approval; when the access ends, the approvals it gave end too. Turn the rule off for a babysitter who should not answer.

### Parental controls and family managers

**Parental controls** is a switch in the profile. Kids rules are enforced by the hub and the integration; whether the device holds a Home Assistant key is set by **Home Assistant access**. Switching parental controls on sets it to **Hub only**: at home the device controls only through the hub, and it gets a read-only key only with remote access, for use away from home, where the integration checks every action. You can choose **Checked** instead; **Direct** is not possible. Parental controls need Halumar Pro in the app on the child's device. Without Pro the child's device shows a notice and controls nothing; the hub and the integration enforce the rules either way. Blocked device types (by default locks, alarm, garage door, gate, cameras), bedtime, rate limit, **Pause everything**, highest volume, color lock and exceptions for single areas or devices then apply and are enforced by the hub. Widgets and Siri need the device's own key (remote access on **Hub only**); a watch also needs the rule **Allow a watch**. App settings open only with the remote PIN. For an exception, use **Parent approval** on the device page: 15 or 30 minutes, 1 hour or **Until tomorrow morning** (end of bedtime). When a change takes keys away or hands them out, the preview names every device it affects.

**Pause everything.** Besides the pause per device, everything pauses for 5 minutes when a child switches more than 4 different devices or more than 12 times within a minute. The values are set per profile; parents may change them unless you fix the rule. A parent approval, a family manager (**End the break**) or the end of the time lifts the pause. To be notified, select the event **Everything paused (too many devices)** under **Notifications**; you get at most one message per child every 15 minutes.

**Family manager** is a right for parents' devices (Halumar Pro on that device), for all children or selected ones. A family manager changes the kids rules of these children in the app, permanently or for a while, but only rules you have not marked **Fixed**; answers their requests (“Can I have more time?”) in the app or from a Home Assistant notification; and sees their activity. Profiles, devices and guests stay with you. Every change appears in the **Log** with its author. The suggestion **Parents** combines Adult with this right for all children. The **Family** page shows who looks after which children, open requests and the family settings.

### Wall panel and devices in Home Assistant

Every paired device except watches and guests appears as a device of the Halumar integration in Home Assistant. The rule **Values in Home Assistant** decides which sensors it gets: **Battery**, **Charging**, **Screen / app active** (wall panels and TVs report the screen state, every other device whether the app is in front), **Brightness** (on phones and tablets only while the app is in front) and **Ambient light (Android)**. The values arrive while the app is connected; no app keeps running in the background just to report.

Wall panels (iPad, Android tablet) and TVs (Apple TV, Android TV) can also be controlled from Home Assistant with the rule **Control from Home Assistant** (on in the suggestions Wall panel, TV and Kids' room panel, off in all others): panel brightness and brightness mode, wake, screensaver, start area and reload; on a TV wake, screensaver, start screen and reload. Only Home Assistant administrators and automations operate these entities, never another device, not even a kids' panel. The card **Device in Home Assistant** on the device page shows the entities and lets you test the commands. Every command appears in the **Log**.

On the panel itself: **Wake from sensors** wakes it when a motion, presence, door or doorbell entity turns on (no automation needed), and **Brightness** can follow an illuminance sensor of Home Assistant or a schedule; dimming lowers the real backlight. Halumar uses no camera, neither for waking nor as a stream.

**Battery protection 20–80 %.** The Halumar integration installs the blueprint **Wall panel battery protection**. In Home Assistant, create an automation from it under **Settings** > **Automations & scenes** > **Blueprints**, choose the panel's battery sensor and a smart plug. The plug switches on at 20 % and off at 80 % (limits adjustable).

### Connection security

The apps talk to the hub only encrypted (port 8100, the certificate is pinned while pairing). To Home Assistant they connect with the address the integration reports. If Home Assistant is reachable at home only over `http://`, the app sends the device's Home Assistant key there only when the home network is proven:

1. the hub with the pinned certificate answers, or
2. the hub is gone, but the app finds Home Assistant in the network with Bonjour, with the same instance ID as when it was paired.

Otherwise it waits and says so. Away from home only `https://` with a checked certificate is used (for example Home Assistant Cloud).

**Always allow unencrypted (less secure)** is a switch in the app's settings for networks you trust; it starts off, and turning it on shows a warning. The profile rule **May always allow unencrypted** decides whether the switch appears. It is fixed off in Wall panel, TV and Kids' room panel; guests and devices with parental controls never get it. With parental controls a device holds a Home Assistant key only with remote access, and that key may only read.

The note **Unencrypted connection** appears only on devices with a profile of kind Person. Children, guests, babysitters, house sitters, wall panels and TVs do not show it; their status window names the connection neutrally. When Home Assistant answers over `http://`, the overview and **Settings** > **Network** show a short hint with a link to the guide on setting up https.

### Log

The **Log** page in the panel shows what happened in your home:

- **Activity**: who switched what and when, at home or away, through which path, and what was refused and why; also panel commands from Home Assistant.
- **Changes**: profiles, adjustments, guests, pairings and set-ups, each with before and after and its author.
- **Alarms**: repeated refused attempts, access alarms (a device's Home Assistant user got more rights and was set back, or a device repeatedly tries to switch directly), a Halumar integration that stops responding, and guests whose access runs out.

Filter by device, person, profile, kind, time and area, and export the log as a CSV file. Only Home Assistant administrators see the log. It is stored in the hub (part of Home Assistant backups) and kept for 7, 30 or 90 days (default 30); older entries are deleted automatically. Under the log's **Settings** you can hide people from the activity. PINs and keys are never in the log.

**Notifications** turn events into messages: choose events (devices paired, set up or removed, access on/off, guests invited, paired, running out or expired, kids at bedtime, paused or asking for a release, activity from away or on selected devices, alarms), targets (Home Assistant notification services such as the Companion app, Telegram or e-mail, which arrive even when Halumar is closed, and Halumar devices, which show them while the app is open), filters, quiet hours and bundling. **Send test** checks a target. Every event also fires the Home Assistant event `halumar_event` for your own automations. Notifications are not an alarm system: if Home Assistant is down, nothing can be sent.

### What profiles do not do

Hiding areas or entities is a display rule; Home Assistant has no read permissions per entity, so a device with a key can read every state. With **Direct**, all rules apply in the Halumar apps, and someone with technical know-how could use the device's key outside Halumar. No profile hides important alerts (alarm, smoke, water). Halumar does not replace an alarm system; alerts appear while the app is open.

### Options

| Option | Default | What it does |
|---|---|---|
| Log level (`log_level`) | `info` | How much the hub writes to its log. Use `debug` when you report a problem. |
| Find the hub automatically (`mdns_enabled`) | on | Announces the hub in your network (Bonjour `_halumar._tcp`), so the apps find it without an address. Off: enter the address in the app. |
| Encrypted connection for the apps (`tls_enabled`) | on | Port 8100 with HTTPS/WSS. The apps pin the certificate fingerprint they learn while pairing. Leave it on. |
| Reset damaged database (`reset_corrupt_database`) | off | Only when the hub reports a damaged database without a usable snapshot, or a database of a different build. The hub starts with an empty database and keeps the old file. Turn it off again afterwards. |
| Reset encryption key (`reset_encryption_key`) | off | Only when the hub reports a damaged or missing key file. Devices that then cannot reach Home Assistant have to be paired again. Turn it off again afterwards. |
| API documentation (`api_docs`) | on | Serves the API documentation to Home Assistant administrators. Turn it off if you do not need it. |
| Pairing QR code in Home Assistant (`pairing_qr_camera`) | `off` | Shows the pairing QR code in the integration's camera **Pairing status**, for example on a dashboard. `guests`: only while a guest pairing is open; a scanned code can only add a guest with the profile and end chosen at the start. `all`: during every pairing. While a code is shown, these can read it: everyone who can sign in to this Home Assistant, every paired device that holds a key, and for 5 to 10 minutes anyone with the camera's image address, without signing in. Any of them can use the one-time code before your guest. A child's device could use the code to add a guest device, and guest devices have no parental controls: with children in the house keep it `off`, or accept that. Prefer `guests`. A device paired with this code is marked in the log. |

Example (YAML mode of the Configuration tab):

```yaml
log_level: info
mdns_enabled: true
tls_enabled: true
reset_corrupt_database: false
reset_encryption_key: false
api_docs: true
pairing_qr_camera: "off"
```

### Network

| Port | Used by |
|---|---|
| 8100/tcp | The Halumar apps (HTTPS/WSS, certificate pinning). The admin page only if you switch on direct access (see below). |
| 8099/tcp | The Halumar integration on this system and the sidebar panel. Administrators cannot sign in over this unencrypted port from the network. |

**Admin page:** you open it through Home Assistant (**Halumar** in the sidebar). Direct access in the home network is off; switch it on under **Settings** > **Advanced** > **Admin page** > **Also open the admin page directly at home** (only in Home Assistant). Then administrators of Home Assistant sign in at `https://<home-assistant>:8100` with their Home Assistant user. Switching it off ends every direct sign-in. Never forward the ports of the hub to the internet.

### Why the hub needs these rights

The app page shows the rights of the hub. This is why it needs them:

- **Host network** (`host_network`): the hub announces itself with Bonjour in your home network, so the apps find it; it puts its network address into the pairing QR code; and it accepts the integration token only from this Home Assistant system. All three need the network of the host.
- **Home Assistant API** (`homeassistant_api`): the hub reads entities, areas and devices of Home Assistant to build your layouts, and it keeps its own `sensor.halumar_*` entities in Home Assistant, which the apps use away from home. Through this API the hub acts with administrator rights in Home Assistant.
- **Supervisor API** (`hassio_api`, default role): the hub reads information about itself and Home Assistant and announces itself to the integration (discovery).
- **Home Assistant login** (`auth_api`): with direct access switched on, administrators sign in to the admin page on port 8100 with their Home Assistant user.
- **Ingress** (`ingress`, `ingress_stream`): the admin page opens in the Home Assistant sidebar, for Home Assistant administrators only.
- **Discovery** (`discovery: halumar`): on every start the hub creates a new token for the integration and hands it over through the Supervisor, so you never copy a token by hand.
- **Watchdog** (`watchdog`): the Supervisor restarts the hub when its health check fails.
- **Supervisor API role**: the hub asks for no `hassio_role`, so it gets the Supervisor's default role. It uses only endpoints that role allows: its own app information, logs and restart, general and Home Assistant information, discovery and the login check.
- **AppArmor** (`apparmor.txt`): the hub ships its own AppArmor profile, which Home Assistant loads instead of the default one. The start scripts (S6 overlay, Bashio) run in the outer profile. The server and the backup hook run in the stricter child profile `hub`: they may read their code and the system libraries, write only to the data folder (`/data`) and `/tmp`, use network sockets (HTTP, WebSocket, Bonjour) and start no other programs.
- **Root in the container**: the server runs as root inside its own container; the start script of the app does not switch to another user. What it may do is limited by the AppArmor profile above and by the rights listed here, not by the user.

### What other Home Assistant users can see

Home Assistant has no per-entity permissions. Every Home Assistant user can read the `sensor.halumar_*` entities of the hub, including the text of notifications sent to a device: the apps read them away from home. Do not send notifications whose text must stay hidden from other Home Assistant users. The same holds for the device values in Home Assistant (battery, charging, screen or app active, brightness). The pairing PIN is never in an entity attribute; the integration's camera shows the pairing QR code only with the option `pairing_qr_camera` (see Options).

### Backups

The hub is part of Home Assistant backups (also while it runs). The encryption key and the TLS key are left out on purpose. So every restore, also on the same system, ends with new keys: all apps and devices have to be paired again.

### Updating

Updates appear in Home Assistant like for any other app. Read the changelog in the update dialog first; changes that need your attention are marked **Breaking**. The hub and the integration of the same version belong together: update both. Before a database migration the hub keeps a snapshot of the old database. Updates never change your profiles or devices.

### Uninstalling

1. Remove the Halumar integration under **Settings** > **Devices & services**. It removes the Home Assistant users of the paired devices and their tokens.
2. Uninstall the app. Home Assistant deletes its data (database, keys, certificate, images).
3. Optional: remove the app repository under **Settings** > **Apps**.

The apps on the devices can then no longer connect; reset them in the app.

### Known limitations

- Home Assistant OS, or another installation with the Supervisor that can run apps, is required; one hub per Home Assistant.
- The apps need the Halumar integration; without it, devices cannot get their Home Assistant user.
- The hub reads from Home Assistant, it never changes your Home Assistant areas.
- Away from home the apps work through Home Assistant (for example Home Assistant Cloud), only on devices whose profile allows remote access; on **Hub only** such a device holds a read-only key, and the integration checks every action. The hub itself is reachable in the home network only, so a device on **Hub only** without remote access works only at home and is offline while the hub restarts.
- Switching is checked in Home Assistant only on **Hub only** and **Checked**; hiding is always a display rule (see **Rules that hold**).
- Devices without a key show cameras as still images through the hub, no live video.
- Devices report their values to Home Assistant only while the app is connected. On phones and tablets the brightness is known only while the app is in front; watches report no values.
- An app cannot switch on the TV set; use the matching integration of Home Assistant for that (for example Apple TV or Android TV Remote).
- Halumar does not send its own push notifications. Requests and notifications reach Halumar devices while the app is open; for messages when the app is closed, choose a Home Assistant notification service.
- A device keeps at most 50 notifications; when there are more, the oldest with the lowest priority go first, **Sticky** ones last. A notification that is not **Sticky** goes 3 days after it was sent or, when scheduled, 3 days after its time.
- A babysitter receives the children's requests only while connected to the hub at home, not away and not through Home Assistant notifications.
- Home Assistant itself does not count VPN addresses (100.64.0.0/10) as home network. A device without remote access therefore cannot reach Home Assistant through Tailscale, even when its range is listed under **Other addresses that count as home**.
- The Halumar apps for iPhone, iPad, Apple TV and Apple Watch are in beta; install them through TestFlight: <https://testflight.apple.com/join/fHX7YT8v>. The apps for Android phone and tablet (wall panel) and Android TV are not available yet.

### Help and bug reports

- Questions and bugs: [github.com/Halumar/hub/issues](https://github.com/Halumar/hub/issues). Add the log of the app (log level `debug`) and the diagnostics of the Halumar integration.
- More documentation (configuration, security model, troubleshooting, API): [docs/README.md](https://github.com/Halumar/hub/blob/main/docs/README.md).

### License

The Halumar Hub is open source under the [GNU Affero General Public License v3.0 only](https://github.com/Halumar/hub/blob/main/LICENSE) (`AGPL-3.0-only`), Copyright 2026 xonatec. It comes with no warranty. You may use, change and share it; whoever distributes a modified hub, or offers it to others over a network, must make its source code available under the same license. Third-party components: [THIRD_PARTY_NOTICES.md](https://github.com/Halumar/hub/blob/main/THIRD_PARTY_NOTICES.md) (also in the app at `/usr/share/doc/halumar/`). The name and logo "Halumar" are trademarks of xonatec ([TRADEMARKS.md](https://github.com/Halumar/hub/blob/main/TRADEMARKS.md)).

---

## Deutsch

Der Halumar Hub gibt jedem Gerät in deinem Zuhause seine eigene, begrenzte Sicht auf Home Assistant: Ein Wandpanel, das Telefon eines Gasts, das Tablet eines Kindes, ein Fernseher oder eine Uhr sehen und steuern nur, was du erlaubst. Er verwaltet deine Halumar-Bereiche, Layouts, gekoppelten Geräte, Profile und Benachrichtigungen und liefert sie an die Halumar-Apps auf iPhone, iPad, Apple TV, Apple Watch, Android-Telefon und -Tablet (auch als Wandpanel) und Android TV. Die Halumar-Apps für iPhone, iPad, Apple TV und Apple Watch sind in der Beta; du installierst sie über TestFlight: <https://testflight.apple.com/join/fHX7YT8v>. Die Apps für Android-Telefon und -Tablet (Wandpanel) und Android TV gibt es noch nicht. Alles verwaltest du im Panel **Halumar** in der Seitenleiste von Home Assistant.

### Installation

1. Repository des Hub hinzufügen: Füge unter **Einstellungen** > **Apps** `https://github.com/Halumar/hub` als App-Repository (Add-on-Repository) hinzu. Oder über diesen Link:

   [![Repository zu deinem Home Assistant hinzufügen.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FHalumar%2Fhub)

2. **Halumar** installieren, **Beim Booten starten**, **Watchdog** und **In Seitenleiste anzeigen** einschalten und starten. Beim ersten Start legt der Hub seine Datenbank, einen Schlüssel für die Geräte-Token, ein TLS-Zertifikat für Port 8100 und fünf Profile an; in Home Assistant ändert er nichts, bis du ein Gerät koppelst.
3. Installiere die **Halumar-Integration** über den Marketplace (Home Assistant 2026.11 und neuer) oder HACS (ältere Versionen). Sie ist nicht im Katalog: Füge sie über [diesen Link](https://my.home-assistant.io/redirect/hacs_repository/?owner=Halumar&repository=integration&category=integration) oder die Adresse `https://github.com/Halumar/integration` als benutzerdefiniertes Repository hinzu und installiere sie. Der Marketplace bittet dich beim ersten Hinzufügen über einen Link, GitHub zu verbinden. Fragt Home Assistant nach einem Neustart, starte es neu. Danach zeigt Home Assistant bei den Geräten und Diensten die Halumar-Integration als gefunden; **Hinzufügen** wählen. Die Integration ist Pflicht: Sie gibt jedem gekoppelten Gerät einen eigenen Home-Assistant-Benutzer.
4. **Halumar** in der Seitenleiste öffnen. Der Assistent **Einrichtung** prüft die Integration und die Adressen von Home Assistant und hilft beim ersten Bereich. Danach Entitäten den Bereichen zuordnen.
5. Gerät koppeln: im Panel **Geräte** > **Gerät koppeln**, **Mitglied** lassen, den **Besitzer** eintragen, wenn das Gerät einer Person gehört, das **Profil** prüfen (vorausgewählt ist **Standard je Gerätetyp**) und **PIN erzeugen**. Die Halumar-App auf dem Gerät öffnen; sie findet den Hub im Netzwerk (Bonjour) und fragt nach der PIN aus dem Panel. Ein Telefon oder Tablet kann stattdessen den QR-Code scannen; am Fernseher gibst du die PIN mit der Fernbedienung ein.
6. Optional, mit einem anderen Gerät einrichten: Ein neues Telefon, Tablet oder ein Fernseher zeigt statt der PIN einen QR-Code und einen 6-stelligen Code. Ein gekoppeltes Telefon oder Tablet (iPhone, iPad, Android-Telefon oder -Tablet), dessen Profil **Neue Geräte einrichten** erlaubt, scannt den Code oder tippt ihn ein, wählt, was das neue Gerät ist, und bestätigt. Siehe **Geräte einrichten**. **Koppeln mit der PIN geht immer.**
7. Optional, Gäste: Einen Gast lädst du selbst unter **Gäste** > **Gast einladen** ein. Oder ein Mitglieder-Gerät, dessen Profil **Neue Geräte einrichten** mit der Art **Gast** erlaubt, zeigt in der App **Gast einladen**. Siehe **Gäste**.
8. Optional, Babysitter oder Haushüter: dem Gerät **Zugangszeiten** geben und seinen Schalter **Zugang** in Home Assistant nutzen. Siehe **Zugangszeiten, Haushüter und Babysitter**.

### Profile

Jedes gekoppelte Gerät hat genau ein **Profil**. Ein Profil hat für jede Regel einen Wert: welche Bereiche das Gerät zeigt, wie viel es bedienen darf, ob es unterwegs funktioniert, welche Funktionen es bekommt (Widgets, Siri, Uhr, Wandpanel) und wie die App aussieht. Änderst du ein Profil, ziehen alle Geräte damit innert Sekunden nach. Profile verwaltest du auf der Seite **Profile** (bis zu 50).

**Vorschläge.** Ab dem ersten Start gibt es fünf Profile: **Erwachsene Person**, **Kind**, **Gast**, **Wandpanel** und **Fernseher**. Unter **Vorschläge** findest du weitere: Einfach bedienen, Kleinkind (bis 5), Kind 6–9, Kind 10–12, Teen (13–17), Babysitter, Haushüter & Helfer, Wandpanel Kinderzimmer und Eltern. **Hinzufügen** kopiert einen Vorschlag in ein neues Profil, das danach deins ist. Updates des Hub ändern nie deine Profile oder Geräte. Bekommt ein Vorschlag eine neue Version, zeigt seine Karte **Neue Version**; **Was ist neu?** nennt die Neuerungen mit Vorschau, und du entscheidest, ob du sie **Übernehmen** willst.

**Art.** Jedes Profil hat eine **Art**: Person, Kind, Gast, Wandpanel, Fernseher, Haushüter oder Babysitter. Die Art ändert keine Regel. Sie legt fest, unter welcher Auswahl das Profil beim Einrichten eines neuen Geräts erscheint und welche Regeln überhaupt gelten (**Neue Geräte einrichten** nur bei Art Person, **Anfragen der Kinder beantworten** nur bei Art Babysitter). Kind hängt nicht an der Kindersicherung: Ein Teen-Profil ohne Kindersicherung hat trotzdem die Art Kind. Geräte der Art Gast, Haushüter und Babysitter bekommen Gastzugang mit Enddatum.

**Fest oder pro Gerät änderbar.** Neben fast jeder Regel steht ein Häkchen **Fest**. Eine feste Regel gilt auf allen Geräten mit diesem Profil gleich und zeigt auf der Geräteseite und in der App ein Schloss. Alle anderen Regeln lassen sich für ein einzelnes Gerät anpassen. Machst du eine Regel fest, entfallen ihre Anpassungen; die Vorschau nennt sie. Darstellungs- und Wandpanel-Regeln können **Am Gerät wählbar** sein: Dann entscheidet die Person am Gerät, das Profil gibt nur den Startwert vor. Jede Regel ist markiert mit **Durchgesetzt** (Home Assistant oder der Hub prüft es) oder **Gilt in Halumar** (die App hält sich daran).

**Ein einzelnes Gerät anpassen.** **Geräte** öffnen und das Gerät wählen; **Wirksame Regeln** zeigt jede Regel und woher ihr Wert kommt. Bei einer Regel **Anpassen** wählen, Wert wählen und wie lange er gilt: **1 Std.**, **Heute**, **Bis morgen früh** (07:00, Zeitzone von Home Assistant), **7 Tage**, ein **Datum** oder **Dauerhaft**. Eine befristete Anpassung endet von selbst; das Gerät schaltet auch offline zurück. **Profilwert verwenden** entfernt eine Anpassung, **Alles auf Profil zurücksetzen** alle. Lockerst du eine Sicherheitsregel (zum Beispiel den Fernzugriff), fragt Halumar noch einmal nach; eine Notiz ist möglich.

**Halumar Pro.** Einige Regeln brauchen Halumar Pro in der Halumar-App auf dem Gerät; der Profil-Editor markiert sie mit **Pro**: **Familienverwaltung**, **Uhr erlauben**, **Widgets, Steuerelemente & Siri**, **Uhr**, **NFC- und QR-Tags**, **Wandpanel-Modus**, **Steuerung aus Home Assistant**, **Aufwecken durch Melder**, **Helligkeit**, **Durchsagen und Gong**, **Bildschirm aus** sowie die **Kindersicherung** mit allen Kinderregeln. Das prüft die App auf dem Gerät; der Hub liefert jede Regel an jedes Gerät und hat keine kostenpflichtigen Funktionen. **Zugang zu Home Assistant** braucht nie Pro.

**Standardprofile.** Auf der Seite **Profile** unter **Standardprofile** wählst du ein Profil pro Gerätetyp (iPhone, iPad, Apple TV, Apple Watch, Android-Telefon, Android-Tablet, Android TV, Gäste) und die Standarddauer für Gäste (1 Tag). Sie gelten überall, wo niemand ein Profil wählt: vorausgewählt beim Koppeln und wenn ein Telefon ein Gerät einrichtet, wenn ein Mitglied einen Gast einlädt, für die eigene Uhr eines Telefons und wenn das Koppeln über die Halumar-Integration startet.

**Erstellen, zuweisen, löschen.** **Neues Profil**: **Aus Vorschlag**, **Aus einem Gerät** (übernimmt seine Werte und Anpassungen) oder **Leer**. Ein Profil weist du auf der Geräteseite zu oder für mehrere Geräte auf einmal, mit oder ohne ihre Anpassungen. Jedes Speichern zeigt zuerst eine Vorschau (**Vorschau & speichern**): welche Geräte sich ändern, welche Anpassungen entfallen, wo ein Home-Assistant-Schlüssel dazukommt oder wegfällt. Zum Löschen eines Profils wählst du ein Ersatzprofil für seine Geräte; das letzte Profil und Standardprofile lassen sich nicht löschen. Der **Verlauf** auf der Geräteseite zeigt jede Änderung mit Person und Zeit.

### Geräte einrichten

Ein neues Gerät fragt nicht, wofür es ist. Es zeigt einen QR-Code und einen 6-stelligen Code (oder nimmt die PIN aus dem Panel). Den Rest entscheidet ein bereits gekoppeltes Telefon oder Tablet (iPhone, iPad, Android-Telefon oder -Tablet): in der App **Einstellungen** > **Gerät einrichten**, Code scannen oder eintippen, wählen, was das neue Gerät ist, und bestätigen.

- **Recht.** Die Profilregel **Neue Geräte einrichten** erlaubt es, für die Arten, die du wählst: Gast, Kind, Wandpanel, Fernseher, Haushüter, Babysitter, Person. Es gibt sie nur bei Profilen der Art Person und nur auf Telefonen und Tablets. Ohne sie richtet ein Gerät nichts ein und lädt niemanden ein. Der Vorschlag **Eltern** erlaubt alle Arten; alle anderen Vorschläge haben es aus. Wie jede Regel kannst du sie fest machen oder für ein Gerät anpassen.
- **Fragen je Art.** Kind: Name, Profil, Bereiche und, bei Familienverwaltung, wer sich um das Kind kümmert. Wandpanel: Ort, Profil, Startbereich, Bereiche. TV: Raum. Gast, Haushüter, Babysitter: Name, Profil, Dauer (höchstens 90 Tage), Fernzugriff und Zugangszeiten, soweit das Profil sie nicht festlegt. Person: Name und Profil.
- **Nie mehr Rechte.** Die App bietet nur Profile an, die dem neuen Gerät nicht mehr geben, als das einrichtende Gerät selbst hat: keinen stärkeren **Zugang zu Home Assistant**, keinen Fernzugriff, den es nicht hat, keine weitere Stufe bei **Von unterwegs**, keine Bereiche, die es nicht sieht. Für dich als Admin gilt diese Grenze nicht.
- **Überblick.** In der App zeigt **Von mir eingerichtet** die Geräte und Gäste, die ein Mitglied eingerichtet hat; dort kann es sie entfernen. Im Panel zeigt jedes Gerät **Eingerichtet von**, und **Geräte** listet **Laufende Einrichtungen** mit dem einrichtenden Gerät; du kannst eine beenden.
- **Grenzen.** Ein Code gilt 5 Minuten und ist mit dem ersten Scan verbraucht. Nach 5 falschen Codes pro Gerät oder 20 am Hub innert 10 Minuten pausiert die Code-Eingabe. QR-Code und Code enthalten kein Geheimnis.

**Uhr.** Ein iPhone richtet seine eigene Apple Watch ein. Halumar auf der Uhr öffnen, **Koppeln** tippen und auf dem iPhone bestätigen. Das Profil des iPhone braucht die Regel **Uhr** und **Neue Geräte einrichten** eingeschaltet (mit beliebiger Art oder ohne Art: dann nur die eigene Uhr). Die Watch wird ein eigenes Gerät mit dem Standardprofil für Apple Watch. Sie hat keine eigene Verbindung zum Hub. Ihr Home-Assistant-Zugang kommt über das iPhone zu ihr, für die Watch verschlüsselt, darum kann das iPhone ihn nicht lesen. Entfernen kannst du die Watch in der iPhone-App mit **Apple Watch entfernen** oder auf ihrer Geräteseite im Panel. Wird ihr iPhone entfernt (von dir oder vom Mitglied, das das iPhone eingerichtet hat), geht die Watch mit: Ohne ihr iPhone bekommt sie keinen Zugang mehr. Das Protokoll nennt das iPhone, mit dem sie ging. Gäste können keine Watch hinzufügen. Ein Gerät mit Kindersicherung kann es, wenn sein Profil die Regel **Uhr erlauben** hat.

### Regeln, die halten

Jedes Profil hat die Regel **Zugang zu Home Assistant**, von streng bis offen:

- **Nur über den Hub**: Ohne Fernzugriff bekommt das Gerät keinen Home-Assistant-Schlüssel. Es liest und schaltet nur über den Hub und sieht nur, was sein Profil erlaubt; der Hub prüft jede Aktion und lässt sie von der Integration mit dem eigenen, nur lesenden Home-Assistant-Benutzer des Geräts ausführen, darum nennt das Logbuch das Gerät. Es funktioniert dort, wo der Hub erreichbar ist, also zu Hause, und ist offline, während der Hub neu startet. Mit Fernzugriff bekommt das Gerät für unterwegs einen Schlüssel, der nur lesen darf: Damit kann es jeden Zustand in Home Assistant lesen, auch was sein Profil ausblendet, und schalten kann es weiterhin nur, was das Profil erlaubt. Die Admin-Seite sagt das überall, wo du Fernzugriff einschaltest. Widgets, Siri, Steuerelemente und eine Uhr brauchen einen solchen Schlüssel, darum gehen sie mit **Nur über den Hub** nur mit Fernzugriff. Kamerabilder bekommt ein Gerät ohne Schlüssel als Standbilder über den Hub; ein Livebild gibt es ohne Schlüssel nicht.
- **Geprüft**: Der Home-Assistant-Benutzer des Geräts darf nur lesen. App, Widgets, Siri und die Uhr schalten über die geprüfte Aktion der Halumar-Integration. Sie stellt bei jedem Befehl vier Fragen: welches Gerät ruft, welches Profil es hat, ob der Aufruf von zu Hause oder von unterwegs kommt (so, wie Home Assistant es für `local_only`-Benutzer entscheidet) und was die Regel sagt. Erst dann schaltet die Integration, und das Logbuch von Home Assistant nennt das Gerät. Bereiche, Bedienung, gesperrte Geräte, Zugangszeiten und die Regeln **Von unterwegs** halten damit auch für jedes andere Werkzeug, das den Schlüssel des Geräts nutzt.
- **Direkt**: Der Home-Assistant-Benutzer des Geräts hat normale Rechte und schaltet direkt. Die Regeln gelten in den Halumar-Apps.

Gast, Haushüter & Helfer, Babysitter, die Kinderprofile und Wandpanel Kinderzimmer beginnen mit **Nur über den Hub**; Gäste und Kinder lassen sich auf **Geprüft** stellen, nie auf **Direkt**. Erwachsene Person, Wandpanel und Fernseher nutzen **Geprüft**, Eltern **Direkt**. Ein Fernseher oder eine Uhr ist nie auf **Nur über den Hub**: Ein solches Gerät bekommt immer mindestens einen Schlüssel, der nur lesen darf, und die Admin-Seite zeigt das. Apple TV, Android TV und Uhren laufen nie ohne Schlüssel, darum gilt **Nur über den Hub** dort als **Geprüft**; TV-Profile bieten es nicht an. Aus demselben Grund richtet ein Gerät mit **Nur über den Hub** einen Fernseher oder eine Uhr nur mit Fernzugriff ein. Die Geräteliste und die Geräteseite zeigen für jedes Gerät die Stufe und seinen Schlüssel (keiner, nur lesen oder voll). Wie jede Regel lässt sich **Zugang zu Home Assistant** fest machen oder pro Gerät anpassen. Hat ein Gerät keinen Schlüssel mehr, lässt der Hub sein Token von der Integration ersetzen. Der alte Schlüssel funktioniert, bis der Hub den Ersatz erhalten und die Integration das alte Token entfernt hat. Solange die Integration nicht erreichbar ist, kann das eine Weile dauern; der Hub versucht es weiter. Bis dahin zeigt die Geräteseite **Schlüssel wird entzogen**.

Die Integration hält eine Kopie der Regeln jedes Geräts, deshalb geht Schalten auch weiter, während der Hub neu startet; nur Kinderregeln, die Aktionen zählen (Takt-Limit, Pause), fragen den Hub. Reagiert die Halumar-Integration nicht, sehen Geräte mit **Geprüft** und **Nur über den Hub** weiter alles, aber Schalten stoppt mit einer klaren Meldung, und Home Assistant zeigt eine Reparatur. Halumar stellt ein Gerät nie von selbst auf **Direkt**. Im Notfall gibst du dem Benutzer des Geräts in der Benutzerverwaltung von Home Assistant mehr Rechte; ist die Integration zurück, stellt sie geprüfte Geräte wieder auf „nur lesen“. Das tut sie auch, wenn jemand einen solchen Benutzer von Hand ändert.

Ausblenden ist eine Anzeigeregel. Home Assistant kennt keine Leserechte pro Entität; ein Gerät mit Schlüssel (**Geprüft**, **Direkt** oder **Nur über den Hub** mit Fernzugriff) kann den Zustand von Entitäten lesen, die sein Profil ausblendet. Nur ein Gerät mit **Nur über den Hub** ohne Fernzugriff sieht nur, was ihm der Hub gibt. Einige Randfunktionen, die Home Assistant nicht prüft, etwa Mitteilungen und Einkaufslisten, deckt die Prüfung nicht ab.

### Von unterwegs

**Fernzugriff** ist die Profilregel **Von unterwegs steuern**: an bei „Erwachsene Person“ und Eltern, aus bei Gast, Haushüter & Helfer, Babysitter, Wandpanel und Fernseher. Er braucht einen Weg zu Home Assistant, den du einrichtest (zum Beispiel Home Assistant Cloud oder ein VPN). Ohne Fernzugriff ist der Home-Assistant-Benutzer des Geräts nur lokal gültig; Home Assistant selbst nimmt ihn dann nur aus deinem Heimnetz an.

Die Regelgruppe **Von unterwegs** legt fest, was ein Gerät darf, wenn es nicht zu Hause ist, pro Geräteart (Schlösser, Garagentore, Licht …) oder für ein einzelnes Gerät: **Bedienen**, **Nur anzeigen** oder **Ausblenden**, jeweils mit eigenem Häkchen **Fest**. Wichtige Meldungen (Alarm, Rauch, Wasser) werden nie ausgeblendet, höchstens nur angezeigt. Mit **Nur über den Hub** und **Geprüft** setzen der Hub und Home Assistant diese Regeln beim Schalten durch; mit **Direkt** gelten sie in den Halumar-Apps.

**Weitere Adressen, die als zu Hause gelten.** Unter **Einstellungen** > **Netzwerk** trägst du weitere Adressbereiche ein, die als zu Hause gelten, etwa die eines VPN; standardmässig ist die Liste leer. **Tailscale** fügt 100.64.0.0/10 hinzu (auf Wunsch auch den IPv6-Bereich); **WireGuard** und **ZeroTier** öffnen die Eingabe mit einem Beispiel; einen eigenen Bereich gibst du mit Namen ein. Nur private Bereiche sind erlaubt, höchstens 10. Apps, Hub, die geprüfte Aktion und die Regeln **Von unterwegs** behandeln diese Adressen als zu Hause, und ein Kopplungs-QR-Code darf einen Hub unter einer solchen Adresse nennen. Auch Mobilfunkanbieter nutzen 100.64.x.x für ihre Kundschaft (CGNAT): füge Tailscale nur hinzu, wenn du es nutzt. Jede Änderung steht im Protokoll.

### Gäste

Gastzugang ist eine Eigenschaft des Geräts, die du beim Koppeln wählst (**Mitglied** oder **Gast**); ändern lässt sie sich später nicht (dafür neu koppeln). Auch Geräte der Art Haushüter und Babysitter werden mit Gastzugang eingerichtet. Ein Gast hat immer ein Ende: 1, 3, 7, 14 oder 30 Tage oder ein Datum, höchstens 90 Tage pro Freigabe, verlängerbar auf der Seite **Gäste** (**+1 Tag**), mit **Verlängern** auf der Geräteseite oder mit der Integrations-Aktion **Gastzugang verlängern**. Gäste laden nie ein, richten keine Geräte ein, öffnen keine App-Einstellungen und nutzen keine Widgets, Siri, Uhr, keinen Wandpanel-Modus und keinen Standort, und sie nutzen **Nur über den Hub** oder **Geprüft**, nie **Direkt**. Alles andere kommt aus dem Profil, auch der Fernzugriff: In den Profilen Gast und Haushüter & Helfer ist er aus, und für einen einzelnen Gast passt du ihn bei Bedarf an. Mit **Nur über den Hub** bekommt das Gerät des Gasts mit Fernzugriff einen Schlüssel, der nur lesen darf (siehe **Regeln, die halten**). Endet der Zugang, stehen alle Sicherheitsregeln auf dem strengsten Wert, und der Hub weist das Gerät ab. Gäste können jedes Profil ohne Kindersicherung nutzen.

**Einladungen durch Mitglieder.** Ein Mitglieder-Gerät, dessen Profil **Neue Geräte einrichten** mit der Art **Gast** erlaubt, zeigt in der App **Gast einladen**: einen grossen QR-Code mit PIN, 5 Minuten und einmal gültig. Das Mitglied wählt Gastprofil, Dauer (höchstens 90 Tage), Fernzugriff und Zugangszeiten, soweit das Profil sie nicht festlegt; es gibt nie mehr weiter, als es selbst hat. Der Gast scannt den Code mit der Handy-Kamera oder tippt die PIN in der Halumar-App ein. Offene Einladungen stehen auf der Seite **Gäste** unter **Offene Einladungen**; dort kannst du sie zurückziehen. Änderungen am Gast machst danach nur du; das Mitglied kann ihn unter **Von mir eingerichtet** entfernen.

### Zugangszeiten, Haushüter und Babysitter

Manche Geräte sollen nur zu bestimmten Zeiten Zugang haben: der Babysitter am Abend, der Haushüter, während ihr weg seid. Zwei Dinge entscheiden, und beide müssen den Zugang erlauben:

- **Zugangszeiten**, eine Profilregel mit Wochenfenstern wie bei der Schlafenszeit (zum Beispiel täglich 17:00–01:00). Sie hat **Fest**, lässt sich pro Gerät anpassen, auch befristet, und beim Einrichten oder Einladen wählen, soweit das Profil sie nicht festlegt. Die Zeiten folgen der Zeitzone von Home Assistant.
- Der Schalter **Zugang** in Home Assistant (`switch.halumar_<name>_access`) für eigene Zeitpläne und Automationen, zum Beispiel aus einem Kalender oder wenn die Alarmanlage scharf ist. Jedes Gerät mit Zugangszeiten oder der Art Gast, Haushüter oder Babysitter hat einen; er ist anfangs an. Bedienen dürfen ihn nur Administratoren von Home Assistant und Automationen; andere Benutzer und die geprüfte Aktion werden abgewiesen. Die Geräteseite im Panel zeigt **Zugang jetzt** mit demselben Schalter (**Zugang erlaubt**).

Ausserhalb seines Zugangs bleibt das Gerät gekoppelt, sieht aber nichts: keine Bereiche, keine Zustände, keine Bedienung. Es zeigt, wann der Zugang wieder beginnt („Zugang wieder ab Fr 17:00“), und öffnet sich dann von selbst. Mit **Nur über den Hub** und **Geprüft** weisen der Hub und Home Assistant jeden Befehl ausserhalb des Zugangs ab; die Integration prüft die Fenster mit ihrer eigenen Uhr, auch wenn der Hub offline ist. Jede Änderung steht im **Protokoll**; das Ereignis **Zugang auf/zu** kann dich benachrichtigen.

**Haushüter & Helfer** (Vorschlag): **Nur über den Hub**, Fernzugriff aus, ausgewählte Bereiche (Wohnzimmer, Küche, Garten und Eingang sind nach Namen vorgewählt), täglich 07:00–21:00; mit eingeschaltetem Fernzugriff Tür, Garage, Tor, Schloss und Alarm von unterwegs nur anzeigen.

**Babysitter** (Vorschlag, Art Babysitter): nur zu Hause, ausgewählte Bereiche (Wohnräume und Kinderzimmer vorgewählt), täglich 17:00–01:00. Mit der Regel **Anfragen der Kinder beantworten** bekommt der Babysitter die Anfragen der Kinder, solange er Zugang hat und zu Hause ist, zusätzlich zu den Familienverwaltern; die erste Antwort gilt. Er darf nur **Ja, 15 Min.**, **Ja, 30 Min.**, **Bis Ende Babysitting** oder **Nein** antworten. Er ändert keine Regeln, gibt nie einen gesperrten Gerätetyp, ein Schloss oder eine feste Regel frei und nie über das Ende seines Zugangs hinaus. Er sieht keine frühere Aktivität, kein Protokoll und keine Regeln. Eltern sehen jede Antwort mit Urheber und können die Freigabe beenden; endet sein Zugang, enden auch seine Freigaben. Soll ein Babysitter nicht antworten, schalte die Regel für ihn aus.

### Kindersicherung und Familienverwaltung

Die **Kindersicherung** ist ein Schalter im Profil. Die Kinderregeln setzen der Hub und die Integration durch; ob das Gerät einen Home-Assistant-Schlüssel hat, legt **Zugang zu Home Assistant** fest. Das Einschalten der Kindersicherung stellt ihn auf **Nur über den Hub**: Zu Hause steuert das Gerät nur über den Hub, und einen Schlüssel, der nur lesen darf, bekommt es nur mit Fernzugriff für unterwegs, wo die Integration jede Aktion prüft. Du kannst stattdessen **Geprüft** wählen; **Direkt** ist nicht möglich. Die Kindersicherung braucht Halumar Pro in der App auf dem Gerät des Kindes. Ohne Pro zeigt das Gerät des Kindes einen Hinweis und steuert nichts; der Hub und die Integration setzen die Regeln so oder so durch. Dann gelten gesperrte Gerätetypen (standardmässig Schlösser, Alarm, Garagentor, Tor, Kameras), Schlafenszeit, Takt-Limit, **Pause für alles**, höchste Lautstärke, Farbsperre und Ausnahmen für einzelne Bereiche oder Geräte, und der Hub setzt sie durch. Widgets und Siri brauchen einen eigenen Schlüssel des Geräts (Fernzugriff bei **Nur über den Hub**); eine Uhr braucht zusätzlich die Regel **Uhr erlauben**. Die App-Einstellungen öffnen sich nur mit der Fern-PIN. Für eine Ausnahme nutzt du die **Eltern-Freigabe** auf der Geräteseite: 15 oder 30 Minuten, 1 Stunde oder **Bis morgen früh** (Ende der Schlafenszeit). Nimmt eine Änderung Schlüssel weg oder gibt sie welche heraus, nennt die Vorschau jedes betroffene Gerät.

**Pause für alles.** Zusätzlich zur Pause pro Gerät pausiert alles für 5 Minuten, wenn ein Kind innert einer Minute mehr als 4 verschiedene Geräte oder mehr als 12-mal schaltet. Die Werte stellst du pro Profil ein; Eltern dürfen sie ändern, solange die Regel nicht fest ist. Eine Eltern-Freigabe, ein Familienverwalter (**Pause beenden**) oder das Ende der Zeit hebt die Pause auf. Willst du benachrichtigt werden, wähle unter **Benachrichtigungen** das Ereignis **Alles pausiert (zu viele Geräte)**; du bekommst höchstens eine Mitteilung pro Kind alle 15 Minuten.

**Familienverwaltung** ist ein Recht für Geräte der Eltern (Halumar Pro auf diesem Gerät), für alle Kinder oder ausgewählte. Wer die Familienverwaltung hat, ändert in der App die Kinderregeln dieser Kinder, dauerhaft oder befristet, aber nur Regeln, die du nicht als **Fest** markiert hast; beantwortet ihre Anfragen („Darf ich noch?“) in der App oder aus einer Home-Assistant-Benachrichtigung; und sieht ihre Aktivität. Profile, Geräte und Gäste bleiben bei dir. Jede Änderung steht mit Urheber im **Protokoll**. Der Vorschlag **Eltern** verbindet „Erwachsene Person“ mit diesem Recht für alle Kinder. Die Seite **Familie** zeigt, wer sich um welche Kinder kümmert, offene Anfragen und die Familien-Einstellungen.

### Wandpanel und Geräte in Home Assistant

Jedes gekoppelte Gerät ausser Uhren und Gästen erscheint in Home Assistant als Gerät der Halumar-Integration. Die Regel **Werte in Home Assistant** legt fest, welche Sensoren es bekommt: **Akku**, **Laden**, **Bildschirm / App aktiv** (Wandpanels und Fernseher melden den Bildschirm, alle anderen Geräte, ob die App vorne ist), **Helligkeit** (auf Telefonen und Tablets nur, solange die App vorne ist) und **Umgebungslicht (Android)**. Die Werte kommen, solange die App verbunden ist; keine App läuft nur fürs Melden im Hintergrund weiter.

Wandpanels (iPad, Android-Tablet) und Fernseher (Apple TV, Android TV) lassen sich mit der Regel **Steuerung aus Home Assistant** auch aus Home Assistant steuern (an in den Vorschlägen Wandpanel, Fernseher und Wandpanel Kinderzimmer, aus in allen anderen): beim Panel Helligkeit und Helligkeitsmodus, Aufwecken, Bildschirmschoner, Startbereich und Neu laden; beim Fernseher Aufwecken, Bildschirmschoner, Startbildschirm und Neu laden. Diese Entitäten bedienen nur Administratoren von Home Assistant und Automationen, nie ein anderes Gerät, auch kein Kinder-Panel. Die Karte **Gerät in Home Assistant** auf der Geräteseite zeigt die Entitäten und testet die Befehle. Jeder Befehl steht im **Protokoll**.

Am Panel selbst: **Aufwecken durch Melder** weckt es, wenn eine Bewegungs-, Präsenz-, Tür- oder Klingel-Entität einschaltet (ohne Automation), und die **Helligkeit** kann einem Helligkeitssensor aus Home Assistant oder einem Zeitplan folgen; gedimmt sinkt die echte Hintergrundbeleuchtung. Halumar nutzt keine Kamera, weder zum Aufwecken noch als Bild.

**Akkuschutz 20–80 %.** Die Halumar-Integration installiert den Blueprint **Wandpanel-Akkuschutz**. In Home Assistant unter **Einstellungen** > **Automationen & Szenen** > **Blueprints** eine Automation daraus erstellen, den Akku-Sensor des Panels und eine Steckdose wählen. Die Steckdose schaltet bei 20 % ein und bei 80 % aus (Grenzen einstellbar).

### Sicherheit der Verbindung

Mit dem Hub sprechen die Apps nur verschlüsselt (Port 8100, das Zertifikat wird beim Koppeln gepinnt). Mit Home Assistant verbinden sie sich über die Adresse, die die Integration meldet. Ist Home Assistant zu Hause nur über `http://` erreichbar, schickt die App den Home-Assistant-Schlüssel des Geräts nur dann dorthin, wenn das Heimnetz nachgewiesen ist:

1. der Hub mit dem gepinnten Zertifikat antwortet, oder
2. der Hub ist weg, aber die App findet Home Assistant per Bonjour im Netz, mit derselben Instanz-ID wie beim Koppeln.

Sonst wartet sie und sagt das. Unterwegs nutzt sie nur `https://` mit geprüftem Zertifikat (zum Beispiel Home Assistant Cloud).

**Unverschlüsselt immer erlauben (weniger sicher)** ist ein Schalter in den App-Einstellungen für Netze, denen du vertraust; er ist anfangs aus und zeigt beim Einschalten eine Warnung. Die Profilregel **Darf „Unverschlüsselt immer erlauben“** legt fest, ob der Schalter erscheint. Bei Wandpanel, Fernseher und Wandpanel Kinderzimmer ist sie fest aus; Gäste und Geräte mit Kindersicherung bekommen ihn nie. Mit Kindersicherung hat ein Gerät einen Home-Assistant-Schlüssel nur mit Fernzugriff, und der darf nur lesen.

Der Hinweis **Unverschlüsselte Verbindung** erscheint nur auf Geräten mit einem Profil der Art Person. Kinder, Gäste, Babysitter, Haushüter, Wandpanels und Fernseher zeigen ihn nicht; ihr Status-Fenster nennt die Verbindung sachlich. Antwortet Home Assistant über `http://`, zeigen dir die Übersicht und **Einstellungen** > **Netzwerk** einen kurzen Hinweis mit Link zur Anleitung für https.

### Protokoll

Die Seite **Protokoll** im Panel zeigt, was in deinem Zuhause passiert ist:

- **Aktivität**: wer wann was geschaltet hat, zu Hause oder unterwegs, über welchen Weg, und was abgelehnt wurde und warum; auch Panel-Befehle aus Home Assistant.
- **Änderungen**: Profile, Anpassungen, Gäste, Kopplungen und Einrichtungen, jeweils mit Vorher und Nachher und Urheber.
- **Alarme**: wiederholt abgelehnte Versuche, Zugriffsalarme (der Home-Assistant-Benutzer eines Geräts bekam mehr Rechte und wurde zurückgesetzt, oder ein Gerät versucht wiederholt, direkt zu schalten), eine Halumar-Integration, die nicht reagiert, und Gäste, deren Zugang abläuft.

Filtern nach Gerät, Person, Profil, Art, Zeitraum und Bereich, Export als CSV-Datei. Das Protokoll sehen nur Administratoren von Home Assistant. Es liegt im Hub (Teil der Home-Assistant-Datensicherung) und bleibt 7, 30 oder 90 Tage (Standard 30); ältere Einträge löscht der Hub automatisch. In den **Einstellungen** des Protokolls blendest du Personen in der Aktivität aus. PINs und Schlüssel stehen nie im Protokoll.

**Benachrichtigungen** machen aus Ereignissen Nachrichten: Ereignisse wählen (Geräte gekoppelt, eingerichtet oder entfernt, Zugang auf/zu, Gäste eingeladen, gekoppelt, laufen bald ab oder abgelaufen, Kinder in der Schlafenszeit, pausiert oder mit einer Anfrage, Aktivität von unterwegs oder an bestimmten Geräten, Alarme), Ziele (Benachrichtigungsdienste von Home Assistant wie die Companion-App, Telegram oder E-Mail, die auch bei geschlossener Halumar-App ankommen, und Halumar-Geräte, die sie bei geöffneter App zeigen), Filter, Ruhezeiten und Bündeln. **Test senden** prüft ein Ziel. Jedes Ereignis löst zusätzlich das Home-Assistant-Ereignis `halumar_event` für eigene Automationen aus. Benachrichtigungen sind keine Alarmanlage: Fällt Home Assistant aus, kann nichts verschickt werden.

### Was Profile nicht leisten

Bereiche oder Entitäten auszublenden ist eine Anzeigeregel; Home Assistant kennt keine Leserechte pro Entität, darum kann ein Gerät mit Schlüssel jeden Zustand lesen. Mit **Direkt** gelten alle Regeln in den Halumar-Apps, und wer sich technisch auskennt, könnte den Schlüssel des Geräts ausserhalb von Halumar nutzen. Wichtige Meldungen (Alarm, Rauch, Wasser) blendet kein Profil aus. Halumar ersetzt keine Alarmanlage; Meldungen erscheinen, wenn die App geöffnet ist.

### Optionen

| Option | Standard | Wirkung |
|---|---|---|
| Protokollstufe (`log_level`) | `info` | Wie ausführlich der Hub protokolliert. `debug`, wenn du ein Problem meldest. |
| Hub automatisch finden (`mdns_enabled`) | an | Kündigt den Hub im Netzwerk an (Bonjour `_halumar._tcp`), damit die Apps ihn ohne Adresse finden. Aus: Adresse in der App eingeben. |
| Verschlüsselte Verbindung für die Apps (`tls_enabled`) | an | Port 8100 mit HTTPS/WSS. Die Apps prüfen den Zertifikats-Fingerabdruck aus der Kopplung. Eingeschaltet lassen. |
| Beschädigte Datenbank zurücksetzen (`reset_corrupt_database`) | aus | Nur wenn der Hub eine beschädigte Datenbank ohne brauchbaren Schnappschuss meldet oder eine Datenbank eines anderen Builds. Der Hub startet mit leerer Datenbank und behält die alte Datei. Danach wieder ausschalten. |
| Verschlüsselungsschlüssel zurücksetzen (`reset_encryption_key`) | aus | Nur wenn der Hub eine beschädigte oder fehlende Schlüsseldatei meldet. Geräte, die danach Home Assistant nicht erreichen, neu koppeln. Danach wieder ausschalten. |
| API-Dokumentation (`api_docs`) | an | Bietet Home-Assistant-Administratoren die API-Dokumentation an. Ausschalten, wenn du sie nicht brauchst. |
| Kopplungs-QR-Code in Home Assistant (`pairing_qr_camera`) | `off` | Zeigt den Kopplungs-QR-Code in der Kamera **Kopplungsstatus** der Integration, zum Beispiel auf einem Dashboard. `guests`: nur, solange eine Gast-Kopplung offen ist; ein gescannter Code kann nur einen Gast mit dem Profil und Ende vom Start hinzufügen. `all`: bei jeder Kopplung. Solange ein Code angezeigt wird, können ihn lesen: alle, die sich bei diesem Home Assistant anmelden können, jedes gekoppelte Gerät mit Schlüssel und 5 bis 10 Minuten lang alle mit der Bildadresse der Kamera, ohne Anmeldung. Sie alle können den einmaligen Code vor deinem Gast nutzen. Ein Kindergerät könnte damit ein Gastgerät hinzufügen; Gastgeräte haben keine Kindersicherung. Mit Kindern im Haus lass die Option auf `off` oder nimm das in Kauf. Lieber `guests`. Ein mit diesem Code gekoppeltes Gerät ist im Protokoll markiert. |

Beispiel (YAML-Modus des Tabs Konfiguration):

```yaml
log_level: info
mdns_enabled: true
tls_enabled: true
reset_corrupt_database: false
reset_encryption_key: false
api_docs: true
pairing_qr_camera: "off"
```

### Netzwerk

| Port | Genutzt von |
|---|---|
| 8100/tcp | Den Halumar-Apps (HTTPS/WSS mit Zertifikats-Pinning). Der Admin-Seite nur, wenn du den direkten Zugang einschaltest (siehe unten). |
| 8099/tcp | Der Halumar-Integration auf diesem System und dem Panel in der Seitenleiste. Über diesen unverschlüsselten Port können sich Administratoren aus dem Netzwerk nicht anmelden. |

**Admin-Seite:** Du öffnest sie über Home Assistant (**Halumar** in der Seitenleiste). Der direkte Zugang im Heimnetz ist aus; einschalten unter **Einstellungen** > **Erweitert** > **Admin-Seite** > **Admin-Seite auch direkt im Heimnetz öffnen** (nur in Home Assistant). Dann melden sich Administratoren von Home Assistant unter `https://<home-assistant>:8100` mit ihrem Home-Assistant-Benutzer an. Ausschalten beendet alle direkten Anmeldungen. Die Ports des Hub nie ins Internet freigeben.

### Warum der Hub diese Rechte braucht

Die App-Seite zeigt die Rechte des Hub. Dafür braucht er sie:

- **Host-Netzwerk** (`host_network`): Der Hub kündigt sich per Bonjour im Heimnetz an, damit die Apps ihn finden; er schreibt seine Netzwerkadresse in den Kopplungs-QR-Code; und er nimmt das Integrations-Token nur von diesem Home-Assistant-System an. Alle drei brauchen das Netzwerk des Hosts.
- **Home-Assistant-API** (`homeassistant_api`): Der Hub liest Entitäten, Bereiche und Geräte von Home Assistant für deine Layouts und pflegt eigene `sensor.halumar_*`-Entitäten, die die Apps unterwegs nutzen. Über diese API handelt der Hub mit Administratorrechten in Home Assistant.
- **Supervisor-API** (`hassio_api`, Standardrolle): Der Hub liest Informationen über sich und Home Assistant und meldet sich bei der Integration an (Erkennung).
- **Home-Assistant-Anmeldung** (`auth_api`): Mit eingeschaltetem direktem Zugang melden sich Administratoren auf der Admin-Seite an Port 8100 mit ihrem Home-Assistant-Benutzer an.
- **Ingress** (`ingress`, `ingress_stream`): Die Admin-Seite öffnet sich in der Seitenleiste von Home Assistant, nur für Administratoren von Home Assistant.
- **Erkennung** (`discovery: halumar`): Bei jedem Start erzeugt der Hub ein neues Token für die Integration und übergibt es über den Supervisor; du kopierst nie ein Token von Hand.
- **Watchdog** (`watchdog`): Der Supervisor startet den Hub neu, wenn seine Statusprüfung fehlschlägt.
- **Rolle in der Supervisor-API**: Der Hub verlangt keine `hassio_role` und bekommt deshalb die Standardrolle des Supervisors. Er nutzt nur Endpunkte, die diese Rolle erlaubt: die eigenen App-Informationen, Protokoll und Neustart, allgemeine und Home-Assistant-Informationen, die Erkennung und die Anmeldeprüfung.
- **AppArmor** (`apparmor.txt`): Der Hub bringt ein eigenes AppArmor-Profil mit, das Home Assistant statt des Standardprofils lädt. Die Startskripte (S6 overlay, Bashio) laufen im äusseren Profil. Server und Backup-Hook laufen im strengeren Unterprofil `hub`: Sie dürfen ihren Code und die Systembibliotheken lesen, nur in den Datenordner (`/data`) und nach `/tmp` schreiben, Netzwerk-Sockets nutzen (HTTP, WebSocket, Bonjour) und keine anderen Programme starten.
- **Root im Container**: Der Server läuft in seinem eigenen Container als root; das Startskript der App wechselt nicht zu einem anderen Benutzer. Was er darf, begrenzen das AppArmor-Profil oben und die hier genannten Rechte, nicht der Benutzer.

### Was andere Home-Assistant-Benutzer sehen

Home Assistant kennt keine Berechtigungen pro Entität. Jeder Home-Assistant-Benutzer kann die `sensor.halumar_*`-Entitäten des Hub lesen, auch den Text von Benachrichtigungen an ein Gerät: Die Apps lesen sie unterwegs von dort. Sende keine Benachrichtigungen, deren Text vor anderen Home-Assistant-Benutzern verborgen bleiben muss. Dasselbe gilt für die Werte der Geräte in Home Assistant (Akku, Laden, Bildschirm oder App aktiv, Helligkeit). Die Kopplungs-PIN steht nie in einem Entitäts-Attribut; die Kamera der Integration zeigt den Kopplungs-QR-Code nur mit der Option `pairing_qr_camera` (siehe Optionen).

### Datensicherung

Der Hub ist Teil der Home-Assistant-Datensicherung (auch während er läuft). Der Verschlüsselungsschlüssel und der TLS-Schlüssel sind bewusst ausgenommen. Darum erzeugt der Hub nach jeder Wiederherstellung neue Schlüssel, auch auf demselben System: Alle Apps und Geräte müssen neu gekoppelt werden.

### Aktualisieren

Updates erscheinen in Home Assistant wie bei jeder anderen App. Lies zuerst das Änderungsprotokoll im Update-Dialog; Änderungen, die deine Aufmerksamkeit brauchen, sind mit **Breaking** markiert. Hub und Integration derselben Version gehören zusammen: aktualisiere beide. Vor einer Umstellung der Datenbank behält der Hub einen Schnappschuss der alten Datenbank. Updates ändern nie deine Profile oder Geräte.

### Deinstallieren

1. Die Halumar-Integration unter **Einstellungen** > **Geräte & Dienste** entfernen. Sie entfernt die Home-Assistant-Benutzer der gekoppelten Geräte und deren Token.
2. Die App deinstallieren. Home Assistant löscht ihre Daten (Datenbank, Schlüssel, Zertifikat, Bilder).
3. Optional: das App-Repository unter **Einstellungen** > **Apps** wieder entfernen.

Die Apps auf den Geräten können sich danach nicht mehr verbinden; setze sie in der App zurück.

### Bekannte Einschränkungen

- Home Assistant OS oder eine andere Installation mit dem Supervisor, die Apps ausführen kann, ist nötig; ein Hub pro Home Assistant.
- Die Apps brauchen die Halumar-Integration; ohne sie bekommen Geräte keinen Home-Assistant-Benutzer.
- Der Hub liest aus Home Assistant und ändert nie deine Home-Assistant-Bereiche.
- Unterwegs arbeiten die Apps über Home Assistant (zum Beispiel Home Assistant Cloud), nur auf Geräten, deren Profil Fernzugriff erlaubt; mit **Nur über den Hub** hat ein solches Gerät einen Schlüssel, der nur lesen darf, und die Integration prüft jede Aktion. Der Hub selbst ist nur im Heimnetz erreichbar, darum funktioniert ein Gerät mit **Nur über den Hub** ohne Fernzugriff nur zu Hause und ist offline, während der Hub neu startet.
- Geprüft wird das Schalten in Home Assistant nur mit **Nur über den Hub** und **Geprüft**; Ausblenden ist immer eine Anzeigeregel (siehe **Regeln, die halten**).
- Geräte ohne Schlüssel zeigen Kameras als Standbilder über den Hub, ohne Livebild.
- Geräte melden ihre Werte nur an Home Assistant, solange die App verbunden ist. Auf Telefonen und Tablets ist die Helligkeit nur bekannt, solange die App vorne ist; Uhren melden keine Werte.
- Eine App kann den Fernseher nicht einschalten; dafür gibt es die passende Integration von Home Assistant (zum Beispiel Apple TV oder Android TV Remote).
- Halumar schickt keine eigenen Push-Mitteilungen. Anfragen und Benachrichtigungen erreichen Halumar-Geräte bei geöffneter App; für Nachrichten bei geschlossener App wählst du einen Benachrichtigungsdienst von Home Assistant.
- Ein Gerät behält höchstens 50 Benachrichtigungen; sind es mehr, gehen zuerst die ältesten mit der tiefsten Priorität, **Dauerhafte** zuletzt. Eine Benachrichtigung, die nicht **Dauerhaft** ist, verschwindet 3 Tage nach dem Senden oder, wenn geplant, 3 Tage nach ihrem Zeitpunkt.
- Ein Babysitter bekommt die Anfragen der Kinder nur, solange er zu Hause mit dem Hub verbunden ist, nicht unterwegs und nicht über Home-Assistant-Benachrichtigungen.
- Home Assistant selbst zählt VPN-Adressen (100.64.0.0/10) nicht zum Heimnetz. Ein Gerät ohne Fernzugriff erreicht Home Assistant deshalb nicht über Tailscale, auch wenn Halumar diese Adressen als zu Hause zählt.
- Die Halumar-Apps für iPhone, iPad, Apple TV und Apple Watch sind in der Beta; du installierst sie über TestFlight: <https://testflight.apple.com/join/fHX7YT8v>. Die Apps für Android-Telefon und -Tablet (Wandpanel) und Android TV gibt es noch nicht.

### Hilfe und Fehler melden

- Fragen und Fehler: [github.com/Halumar/hub/issues](https://github.com/Halumar/hub/issues). Bitte das Protokoll der App (Stufe `debug`) und die Diagnosedaten der Halumar-Integration anhängen.
- Weitere Dokumentation (Konfiguration, Sicherheitsmodell, Fehlersuche, API; Englisch): [docs/README.md](https://github.com/Halumar/hub/blob/main/docs/README.md).

### Lizenz

Der Halumar Hub ist Open Source unter der [GNU Affero General Public License v3.0 only](https://github.com/Halumar/hub/blob/main/LICENSE) (`AGPL-3.0-only`), Copyright 2026 xonatec. Er kommt ohne Gewährleistung. Du darfst ihn nutzen, ändern und weitergeben; wer einen veränderten Hub weitergibt oder anderen über ein Netzwerk anbietet, muss dessen Quellcode unter derselben Lizenz zugänglich machen. Komponenten Dritter: [THIRD_PARTY_NOTICES.md](https://github.com/Halumar/hub/blob/main/THIRD_PARTY_NOTICES.md) (auch in der App unter `/usr/share/doc/halumar/`). Name und Logo „Halumar“ sind Marken von xonatec ([TRADEMARKS.md](https://github.com/Halumar/hub/blob/main/TRADEMARKS.md)).
