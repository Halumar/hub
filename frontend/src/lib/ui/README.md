# `src/lib/ui` – design system of the admin UI

Rules, tokens, classes and components of the hub's SvelteKit admin UI.

## Rules

1. **No `style="…"` attributes.** Use the component classes from `src/app.css` and Tailwind utilities with the token colors (`bg-surface`, `text-ink-2`, `border-line`, `text-err-ink`, …). Only data-driven values that cannot be a class (progress width, a user-chosen area color, a computed menu position) may use the `style:prop={value}` directive. Component-local CSS goes into a `<style>` block using the CSS variables.
2. **Colors only via tokens** (`var(--ink)`, `var(--surface)`, …). Never hard-coded hex values in markup (exception: QR code on white). Light and dark come from `<html data-theme>` (see `$lib/theme.svelte.js`); never set `color-scheme` by hand.
3. **All UI text through `t()`** from `$lib/i18n/index.svelte.js`. Keys live in `src/lib/i18n/{en,de}/<namespace>.js`; EN and DE must have the same keys (test `e2e/design.spec.js`). The product name is `{brand}` (from the generated `brand.ts`), never a literal. Dates/times via `$lib/utils.js` (`formatDate`, `formatRelativeDate`, `formatTimeRemaining`, `formatWeekdayTime`), which follow the UI language and the local time zone.
4. **Accessibility:** every control has a visible label (`Field` → `<label for>`), icon-only buttons have `aria-label`, status is never color alone (text + dot), toggles are `Switch` (`role=switch`), segment controls are `Segmented` (`aria-pressed`, immediate toggles) or `RadioSeg` (`role=radiogroup`, arrow keys, choices inside a form), tabs are `Tabs` (`role=tab`, arrow keys), dialogs are `Modal` (focus trap, Esc, focus return). Touch targets: controls are 36 px with a mouse and 44 px on touch/phones (`--h-ctl`). `svelte-check` must report 0 warnings; `svelte-ignore` only for backdrop clicks that have a keyboard equivalent (Esc).
5. **States:** first load → `LoadingState`; later reloads silent (no spinner, keep scroll); load failure → `ErrorState` with retry, never together with an empty state; empty → `EmptyState` with the next action. Feedback via `addToast` (bottom left) or inline `.saved` ("Saved").
6. **Clickable rows/cards** (list → detail): the whole row opens the detail via `RowLink` (stretched link, see below), never a separate "Edit"/"Details" button for the primary action. Put every control of the row into one `.rowlink-ctl` wrapper (it sits above the link and never navigates, also on touch), add a `.rowlink-chev` chevron as affordance, and move secondary actions ("Properties", copy, delete) into the `Menu`.
7. **Responsive:** phones (< 640 px) get single-column layouts, modals become bottom sheets, tables become card lists, primary actions sit in reach (sticky bottom bar on long forms). No horizontal scrolling at 390 px.

## Tokens (`src/app.css`)

| Group | Tokens |
|---|---|
| Surfaces | `--bg`, `--bg-2`, `--surface`, `--surface-2`, `--fill`, `--fill-2`, `--fill-3`, `--line`, `--line-2`, `--scrim` |
| Text | `--ink` (17:1), `--ink-2` (8:1), `--ink-3` (5.6:1) |
| Brand | `--accent` (coral, surfaces/focus), `--accent-ink` (text, 5.8:1), `--accent-soft`, `--navy` |
| Logo | `--mark-house`, `--mark-window`, `--mark-word`, `--mark-offline` (brand colors, light and "on dark"); only for `BrandMark`/`BrandWordmark` |
| Status | `--ok/-ink/-soft`, `--warn/-ink/-soft`, `--err/-ink/-soft`, `--info/-ink/-soft` (`*-ink` on `*-soft` ≥ 4.6:1) |
| Type | `--fs-cap` 12, `--fs-sm` 13, `--fs-body` 14, `--fs-t3` 15, `--fs-t2` 18, `--fs-t1` 24, `--fs-kpi` 28; `--font`, `--mono` |
| Space | `--s1`…`--s10` (4-pt) |
| Radius | `--r-xs` 6, `--r-sm` 8, `--r-ctl` 10, `--r-box` 12, `--r-card` 14, `--r-modal` 20 |
| Size | `--h-ctl` / `--h-ctl-sm` (36/30 px, 44 px on touch), `--sidebar-w` |

## Classes (`src/app.css`, layer components)

- Text: `t-page` (h1), `t-section` (h2), `t-card`, `t-sub`, `t-help`, `t-label`, `t-overline`, `t-mono`, `t-muted`, `t-err`, `link`, `sr-only`
- Layout: `page`, `page-narrow`, `page-head`, `stack`, `row`, `spacer`, `grid-cards`
- Buttons: `btn` + `btn-primary` | `btn-danger` | `btn-danger-solid` | `btn-ghost`, sizes `btn-sm`, `btn-icon`, `btn-block` (works on `<a>` too)
- Forms: `field`, `field-label`, `field-error`, `input`, `select`, `textarea`, `input-mono`, `check`
- Choice: `seg` (segmented), `opts`/`opt` (option tiles), `chip` (toggle tags, `aria-pressed`)
- Containers: `card`, `card-flush`, `card-head`, `box`, `dl`, `danger-zone`, `kpi`, `chipic`
- Status: `badge` + `badge-ok|warn|err|info|guest|accent` (with `<span class="dot">`), `avatar`, `progress`, `saved`
- Banners: `banner banner-info|ok|warn|error|neutral` (prefer the `Banner` component)
- Tables/lists: `table-wrap` + `table` (`tr.group`, `tr.clickable`), `list`
- Clickable rows: `rowlink` (row/card, `position: relative`), `rowlink-target` (set by `RowLink`), `rowlink-ctl` (controls above the overlay), `rowlink-chev` (chevron, follows hover/focus). Hover only with a real pointer, pressed (`:active`) and focus ring (2 px, inset) on the whole row
- Tabs: `tabs`, `tab` (prefer `Tabs`)
- Feedback: `spinner`, `spinner-sm`, `state`, `skeleton`, `toast`
- Overlay: `modal*` (use `Modal`), `menu`, `menu-item` (use `Menu`)

## Components

| Component | Use |
|---|---|
| `Icon` | `<Icon name="trash" />`, names in `icons.js`; decorative unless `label` |
| `Modal` | `bind:open`, `title`, `description`, `size` sm/md/lg/xl, `onclose`, `dismissible`, snippets `children`, `footer({ close })`, `header({ titleId, close })`, `testid`. Initial focus: `[data-autofocus]` > first field > first focusable |
| `ConfirmDialog` (`$lib/components`) | `bind:open`, `title`, `message`, `confirmLabel`, `danger`, `confirmText` (type-to-confirm), `onconfirm`; focus starts on Cancel |
| `Field` | `label`, `hint`, `error`, `required`, `id`; snippet `children({ id, describedby, invalid })` renders the control |
| `Switch` | `bind:checked`, `label`, `description`, `disabled`, `onchange(next)` (return `false` to revert), `testid` |
| `Segmented` | `options [{ value, label, ariaLabel?, title? }]`, `bind:value`, `label` (group name), `onchange` |
| `RadioSeg` | `options [{ value, label, title?, disabled? }]`, `value`, `label` or `labelledby`, `onchange(value)`, `invalid`, `size` – radio group with one tab stop, arrows/Home/End select |
| `Tabs` | `items [{ id, label, icon?, badge?, testid? }]`, `bind:value`, `label`, `onchange`; children = active panel |
| `Banner` | `tone` info/ok/warn/error/neutral, `title`, children, snippet `actions` |
| `EmptyState` / `ErrorState` / `LoadingState` | page/card states |
| `Menu` | overflow menu: `items [{ label, icon, onclick, danger, disabled, testid } | { separator: true }]`, `label`, `ariaLabel`, `iconOnly`, `buttonClass` |
| `RowLink` | primary link of a `.rowlink` row/card: `href`, `testid`, children = visible name. Real `<a>` (Cmd/middle click, one link per row for screen readers), Space activates like Enter, not draggable |
| `PageHeader` | `title` (h1), `subtitle`, `back` (href), snippet `actions` |
| `StatusBadge` | `online` → "Online"/"Offline" with dot |
| `Avatar` | owner initial (decorative) |
| `BrandMark` | logo symbol from the brand outlines: `size` (px; under 32 the small cut), `state` idle/loading (window breathes 0.82–1, 1.6 s, static with reduced motion)/offline (window outline, 45 %), `label` |
| `BrandWordmark` | the wordmark as outlines, never set text, `height`. Never beside or above `BrandMark`: the h of the wordmark is the mark (a name gets the wordmark alone, an icon the mark alone). Geometry in `brand-mark.js` is generated, never edited |

Global: `Toast` (store `$lib/stores/toast.svelte.js`), `WsBadge`, `PairingDialog` (open with `openPairing(role)` from `$lib/stores/pairing.svelte.js`, roles `member` | `guest`).
