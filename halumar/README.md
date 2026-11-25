# Halumar Hub

Gives every device in your home its own, limited view of Home Assistant: a wall panel, a guest's
phone, a child's tablet, a TV or a watch each see and control only what you allow. The hub is the
app (add-on) behind the Halumar apps for iPhone, iPad, Apple TV, Apple Watch, Android phone and
tablet (wall panel) and Android TV.

## What it does

- One admin page in the Home Assistant sidebar: areas, entity tiles and widgets, with a preview per
  device type.
- A profile per device with its rules: which areas it shows, how much it may control, whether it
  works away from home, how the app looks. Fix a rule for every device of a profile or adjust it
  for one device, also for a limited time.
- Guests with an end date, and access hours for babysitters and house sitters.
- Parental controls that you switch on per profile (Halumar Pro in the app on the child's device),
  and family managers who answer the children's requests.
- Each paired device gets its own Home Assistant user without admin rights, so the logbook shows
  which device did what. Guests, babysitters, house sitters, children and the Kids' room panel
  start on **Hub only**: without remote access their device gets no Home Assistant key and sees
  only what its profile allows.

## Who it is for

Home Assistant users who want to hand a device to someone (family, guests, a babysitter) or mount
one on a wall, without giving it the full Home Assistant.

## What you need

- Home Assistant OS, or another installation with the Supervisor that can run apps; Home
  Assistant 2025.11 or later, on `amd64` or `aarch64`.
- The Halumar integration of the same version, from the Marketplace (Home Assistant 2026.11 and
  newer) or HACS (older versions), as the custom repository `https://github.com/Halumar/integration`.
- The Halumar app on each device. The Halumar apps for iPhone, iPad, Apple TV and Apple Watch
  are in beta; install them through TestFlight: <https://testflight.apple.com/join/fHX7YT8v>.
  The apps for Android phone and tablet (wall panel) and Android TV are not available yet.

## Next steps

Install and start this app, then open the **Documentation** tab. It walks you through installing
the integration, the **Setup** assistant in the **Halumar** panel and pairing your first device.

---

## Deutsch

Gibt jedem Gerät in deinem Zuhause seine eigene, begrenzte Sicht auf Home Assistant: Ein Wandpanel,
das Telefon eines Gasts, das Tablet eines Kindes, ein Fernseher oder eine Uhr sehen und steuern nur,
was du erlaubst. Der Hub ist die App (Add-on) hinter den Halumar-Apps für iPhone, iPad, Apple TV,
Apple Watch, Android-Telefon und -Tablet (Wandpanel) und Android TV. Du ordnest Bereiche, Entitäten und Widgets im
Halumar-Panel an, gibst jedem Gerät ein Profil mit Regeln, lädst Gäste befristet ein und schaltest
bei Bedarf die Kindersicherung ein.

Du brauchst Home Assistant OS oder eine andere Installation mit dem Supervisor, die Apps
ausführen kann (Home Assistant 2025.11 oder neuer), und die Halumar-Integration derselben
Version aus dem Marketplace (Home Assistant 2026.11 und neuer) oder HACS (ältere Versionen).
Die Halumar-Apps für iPhone, iPad, Apple TV und Apple Watch sind in der Beta; du installierst sie
über TestFlight: <https://testflight.apple.com/join/fHX7YT8v>. Die Apps für Android-Telefon und -Tablet
(Wandpanel) und Android TV gibt es noch nicht.
Weiter geht es im Tab **Dokumentation**.

---

The Halumar Hub is free software under the AGPL-3.0-only. Halumar is an independent project, not
affiliated with or endorsed by Home Assistant or the Open Home Foundation.
