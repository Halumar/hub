# `src/routes` SvelteKit pages

Client-rendered admin UI of the Halumar hub (`prerender = true`, `ssr = false`). Pages use the design system in `$lib/ui`, texts via `$lib/i18n`, data via `$lib/api.js`, live updates via `onBroadcast(...)`.

## Structure

| File | Purpose |
|------|---------|
| `+layout.svelte` | Shell: auth gate (Ingress or session login form), sidebar (Overview, Areas, Devices, Profiles, Guests with active-guest count, Family, Log with unread-alarm count, Notifications, Settings), phone drawer with focus handling, slim header under Ingress, offline banner, live badge, version, pairing dialog, toasts; initializes i18n and theme. |
| `+page.svelte` | Overview: KPIs (devices online without admin sessions, areas, active guests, open notifications), integration/setup warnings, setup checklist (`$lib/components/dashboard/SetupCard`: one line once complete, opens by hand, localStorage `hub.dash.setup`), "Recent important events" (`RecentImportant`, logic `$lib/log/important.js`) next to the devices, areas. |
| `areas/+page.svelte` | Area list: create, import from Home Assistant, edit (shared modal), copy, delete (confirm), active switch, reorder by buttons or drag. |
| `areas/[id]/+page.svelte` | Area editor controller: tile panel (a click on a tile, Enter/Space or "…" → "Customize tile…" opens it, discard question on unsaved changes), quiet reloads, "Saved" state, DnD model (`buildDndSections`), reorder by buttons/menus (`moveSection`, `moveItem` → `reorderSections` / `updateLayout`), entity/widget/section actions. |
| `areas/[id]/components/*` | `QuickAccessSection` (quick access level of an entity in the apps), `RoomClimateCard` (which entity gives the room temperature and humidity), `AreaHeader` (title, actions, toolbar), `SectionGrid` (sections, svelte-dnd-action on desktop), `EntityCard` / `WidgetCard` (tiles; rows with "…" menu on phones), `PreviewPanel` (iPhone preview), `TilePanel` + `TilePreview` + `TileIconPicker` (tile settings: side panel with live preview iPhone/Apple TV in both card styles, curated icons + MDI search + SF name, phone: full-screen sheet; logic in `$lib/data/tiles.js`), dialogs (`AddEntityModal` windowed list, `AddSectionModal`, `AddWidgetModal`, `EditWidgetModal`, `WidgetFields`, `SyncFromHaModal`, `RenameModal`). |
| `clients/+page.svelte` | Devices: table grouped by owner (or flat), search incl. HA user, status as text, profile and adjustments per device, remote access as text (from the profile); selection → change together (assign profile, own area, adjust/remove a rule, with preview); cards on phones. |
| `clients/[id]/+page.svelte` | Device: profile section (`DeviceProfile`), HA user card (owner, token renewal, setup state), notifications, details (remote access, guest expiry with extend), activity, danger zone. |
| `clients/[id]/components/DeviceProfile.svelte` | Profile of the device: profile chip with "Choose profile" (preview), access, own area, summary, kids status and parent release, remote PIN (profile/own), effective rules, reset all, profile from device, history. |
| `clients/[id]/components/EffectiveRules.svelte` | Effective rules with origin (filter all / adjusted / fixed), adjust / use profile value / change in profile. |
| `clients/[id]/components/FamilyCard.svelte` | Family manager of a device: the right, the children it looks after, its HA notify targets. |
| `clients/[id]/components/PanelCard.svelte` | The device in Home Assistant: last status, its entities, test buttons on wall panels and TVs. |
| `guests/+page.svelte` | Guests: expiry bar and local time, profile and remote access, +1 day, rules (device page), end access, default profiles + guest duration, invite (pairing dialog with access guest). |
| `profiles/+page.svelte` | Profiles: equal cards, "New profile" (three tiles), default profiles per device type + guest duration, gallery of suggestions (copy, "already added", new version). |
| `profiles/[id]/+page.svelte` | Profile editor: groups with search, value + "Fixed" (+ "User's choice") per rule, boundaries with lock and "Why?", enforcement, kids rules and exceptions, "When away", profile remote PIN, save only with preview (dry run, loosening confirmation), new suggestion version, duplicate, delete with replacement. |
| `family/+page.svelte` | Family: open requests of the children with Yes / No, the requests of the last 24 hours, who looks after which children, house settings of the requests. |
| `log/+page.svelte` | Log: timeline of activity, changes and alarms by day, filters, alarm banner, detail panel. |
| `log/notify/+page.svelte` | Notifications at events: templates, events, targets, quiet hours, bundling, test send. |
| `log/settings/+page.svelte` | Log settings: retention, hide people, alarm threshold, CSV export, privacy notes. |
| `setup/+page.svelte` | Setup assistant: integration, connection, addresses, first area, first device, finish. |
| `notifications/+page.svelte` | Notifications per device with status text and details; compose dialog (`?compose=1` opens it). |
| `settings/+page.svelte` | Tabs General / Network / Advanced / System & logs (`?tab=`); page-level banner for settings that failed to load. |
| `manifest.webmanifest/+server.js` | Web app manifest of the admin UI (name from the brand, icons). |
| `settings/settings-state.svelte.js` | Shared state and loaders of the settings tabs. |
| `settings/components/*` | `GeneralTab` (admin UI language/theme, HA URLs, hub URL, home screen, control mode, API), `NetworkTab` (Home Assistant addresses, hub address, further home networks; templates in `home-networks.js`), `AdvancedTab` (HA users, integration gates, tokens, backup/import, danger zone), `ScanTagsCard` (NFC and QR tags), `AuditLog` (admin changes and security events), `SystemTab` (health, integration, TLS, logs, restart, license and source code). |
