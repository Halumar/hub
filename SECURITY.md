# Security policy

## Reporting a vulnerability

Please do not report security problems in public issues, discussions or pull requests.

Use GitHub's private vulnerability reporting: **Report a vulnerability** on the
[Security tab](https://github.com/Halumar/hub/security/advisories/new) of this repository. Please
include:

- the affected version of the hub and, if relevant, of the integration and of the Halumar app on the device,
- what an attacker could do, and from where (home network, a paired device, another app on the
  same system, the internet),
- the steps to reproduce, ideally with a short proof of concept.

Never send real access tokens, passwords, pairing PINs, QR codes or backups. Replace them with
placeholders.

We confirm your report, keep you informed about the assessment and the fix, and agree with you on
when and how the issue is disclosed.

## Supported versions

Security fixes are released for the latest version of the hub only.
Hub and integration of the same version belong together; update both.

## Scope

In scope:

- the hub app in this repository: its REST and WebSocket API, the admin page, pairing and device
  setup, the storage of secrets, the image and its AppArmor profile,
- the enforcement of profile rules that the hub is responsible for (see the
  [security model](docs/security-model.md)).

Out of scope:

- the limits described in the [security model](docs/security-model.md), for example that Home
  Assistant has no read permissions per entity,
- problems in the [Halumar integration](https://github.com/Halumar/integration) (report them
  there) or in the [contract](https://github.com/Halumar/contracts),
- problems in Home Assistant itself or in other apps and integrations; they belong to their
  projects, for example the [Home Assistant security policy](https://www.home-assistant.io/security/),
- setups that expose the hub's ports to the internet.

## Admin access

The admin page opens through Home Assistant (Ingress). Direct access in the home network
(`https://<home-assistant>:8100`, Home Assistant administrators only) is off by default and can
only be switched on from Home Assistant; switching it off ends every direct sign-in. Never forward
the hub's ports to the internet.
