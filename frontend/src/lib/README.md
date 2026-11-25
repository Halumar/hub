# `src/lib` Frontend shared library

Shared layer of the Halumar hub admin UI (SvelteKit, Svelte 5 runes): API and WebSocket clients, stores, the design system (`ui/`), i18n, theme and shared components. Routes import everything via `$lib`.

## Structure

| Path | Purpose |
| --- | --- |
| `api.js` | REST client (`api` object): CSRF, retries (GETs only on network errors), Ingress base path, `resolveImageSrc`. |
| `ws.js` | Admin WebSocket (`ws` singleton): started after login, answers pings, reports auth state and broadcasts. |
| `bulk.js` | Several devices at once (`POST /pairing/clients/bulk`): actions, dry-run summary. |
| `device-info.js` | Client info of a device: platform labels and icons, app version line, activity feed. |
| `ha-cleartext.js` | Whether Home Assistant answers the apps over http (for the calm hint). |
| `setup.js` | Setup assistant: steps, done detection from live data, auto-open rule. |
| `sync-result.js` | What a sync from Home Assistant left out, as one short line. |
| `utils.js` | Formatting in the UI language and local time (`formatDate`, `formatRelativeDate`, `formatWeekdayTime`, `formatTimeRemaining`, `formatHoursShort`, `formatDuration`), `toLocalInput`, `debounce`, `copyText`, `clientName`. |
| `device-user.js` | HA user name of a device (`deviceUserName`) and the "setup takes long" check. |
| `ha-context.js` | Reads `hass` (language, dark mode) from the HA frontend when running under Ingress, and watches it. |
| `theme.svelte.js` | Light/dark: preference `auto/light/dark` (localStorage `hub.theme`); auto follows HA, then `prefers-color-scheme`; sets `<html data-theme>`. |
| `i18n/` | `t(key, params)`, plural rules, `{brand}`; dictionaries per namespace in `en/` and `de/` (identical keys, checked by `e2e/design.spec.js`). Preference `auto/en/de` (localStorage `hub.lang`). |
| `ui/` | Design system: components and rules, see `ui/README.md`. |
| `stores/ws-store.svelte.js` | Reactive `wsConnection` and `onBroadcast(type, cb)`; pseudo event `resync` after a reconnect. |
| `stores/toast.svelte.js` | `addToast`, `dismissToast`, `clearToasts`. |
| `stores/pairing.svelte.js` | Pairing dialog state: `openPairing(role)` with `member` / `guest`. |
| `components/PairingDialog.svelte` | Pairing dialog (QR, PIN, countdown, TLS address and fingerprint; access member/guest, owner, profile tiles with the default per device type, own area, guest duration up to 90 days). Mounted once in the layout; closing cancels an active PIN. |
| `components/EditAreaModal.svelte` | The one "Area properties" dialog (area list "…" menu, editor header); PATCHes only changed fields. |
| `profiles/*` | Profile model: `catalog.svelte.js` (rule catalog from `GET /profile-rules`, DE/EN texts, reasons, enforcement), `format.js` (value choices and texts, loosening check, summary sentence), `context.js` (areas, tags, entities for the editors), `errors.js` (texts for the API error codes), `scene-hints.js` (scenes and scripts whose targets a profile may not switch). |
| `log/*` | Log tab: `describe.js` (entry texts for timeline, detail and CSV), `important.js` ("Recent important" on the overview), `rows.js` (rows of "Notifications at events"), `state.svelte.js` (shared filters). |
| `components/profiles/*` | Profile UI: `RuleValueEditor` (one rule value by type), `RemoteControlEditor` ("When away"), `KidsExceptionsEditor`, `ProfileCard`, `ProfilePicker` (equal tiles), `PreviewView` + `LooseningWarning` (dry-run preview), `AdjustDialog` (adjust with duration), `AssignProfileDialog`, `DeleteProfileDialog`, `NewProfileDialog`, `DefaultProfilesCard`, `RemotePinDialog`, `SceneHints`, `SourceBadge`, `ProfileHistory`, `ProfileIcon`. |
| `components/kids/*` | `RuleEditor` (kids rules of one level, used for exceptions) and `QuietHoursEditor` (seven equal weekday buttons). |
| `components/BulkEditDialog.svelte` | Change several devices together: choose, preview (dry run), apply, result. |
| `components/DeleteDeviceDialog.svelte` | Remove a device: online confirmation, offline choice between next connect and at once. |
| `components/HaCleartextHint.svelte` | Calm hint when Home Assistant is reached over http. |
| `components/OnboardingSessions.svelte` | Devices waiting to be set up from another device. |
| `components/dashboard/*` | `SetupCard` (setup checklist on the overview) and `RecentImportant` (recent important log entries). |
| `components/log/*` | `LogHeader`, `EntryPanel` (detail of an entry), `ExportDialog` (CSV export), shared `log.css`. |
| `components/ConfirmDialog.svelte` | Confirmation on `Modal`; focus on Cancel; optional type-to-confirm word. |
| `components/IconPicker.svelte` | MDI search + free text, lazy MDI catalog, keyboard navigation. |
| `components/Toast.svelte`, `WsBadge.svelte`, `LoadError.svelte` | Toast stack (polite live region), honest live indicator, "value unknown" state. |
| `data/domains.js` | Domain list for filters, `domainIcon()`, widget types (labels via i18n). |
| `data/kids.js` | Kids mode: rule keys per level, inheritance (`effective`), summaries (`summarize`, `daysLabel`, `windowLabel`), type and reason labels, editor validation. |
| `data/tiles.js` | Tile settings per area entity: helpers for the tile panel and its preview. |
| `data/mdi-*.js` | MDI catalog (lazy chunk) and lookup. |
| `brand.ts` | Generated from `contracts/brand.json`; never edit. |

## Conventions

- All visible text via `t()`; the product name is `{brand}`.
- No `style="…"` attributes; see `ui/README.md` for tokens, classes and components.
- First load shows `LoadingState`; later reloads are silent and keep the scroll position; a failed load shows `ErrorState`, never an empty state.
