import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';
import { BRAND } from '../src/lib/brand.ts';

// The dev server compiles the profile pages on first use; give the first look more time.
test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * Admin UI of the profile model against the in-memory hub
 * (e2e/profiles-hub.js, built after the contract, openapi/hub.openapi.json tag
 * profiles).
 */

async function boot(page, opts = {}, lang = 'de') {
	const hub = createProfilesHub(opts);
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
	await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
	return { hub, errors };
}

/** Equal sizes: every element of the locator has the same width and height (±1.5 px). */
async function expectEqualSizes(locator, { width = true, height = true } = {}) {
	const boxes = await locator.evaluateAll((els) => els.filter((e) => e.offsetParent !== null).map((e) => e.getBoundingClientRect()).map((r) => ({ w: r.width, h: r.height })));
	expect(boxes.length).toBeGreaterThan(1);
	for (const b of boxes) {
		if (width) expect(Math.abs(b.w - boxes[0].w)).toBeLessThanOrEqual(1.5);
		if (height) expect(Math.abs(b.h - boxes[0].h)).toBeLessThanOrEqual(1.5);
	}
}

async function noHorizontalScroll(page) {
	const over = await page.evaluate(() => {
		const main = document.querySelector('main');
		return Math.max(document.documentElement.scrollWidth - window.innerWidth, main ? main.scrollWidth - main.clientWidth : 0);
	});
	expect(over).toBeLessThanOrEqual(0);
}

const writes = (hub, method, prefix) => hub.requests.filter(([m, p]) => m === method && p.startsWith(prefix));

test.describe('Profiles – list and gallery', () => {
	test('navigation shows Profiles, no Kids page and no Templates tab', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles');
		await expect(page.getByTestId('nav-profiles')).toBeVisible();
		await expect(page.getByTestId('nav-kids')).toHaveCount(0);
		await page.goto('/settings');
		await expect(page.getByRole('tab', { name: 'Vorlagen' })).toHaveCount(0);
	});

	test('five start profiles as equal cards, defaults and gallery with 14 suggestions', async ({ page }) => {
		const { errors } = await boot(page);
		await page.goto('/profiles');
		const cards = page.getByTestId('profile-card');
		await expect(cards).toHaveCount(5);
		await expectEqualSizes(cards);
		await expect(page.getByTestId('profile-list')).toContainText('Erwachsene Person');
		await expect(page.getByTestId('profile-list')).toContainText('Fernseher');
		await expect(cards.filter({ hasText: 'Fernseher' }).getByTestId('profile-badge-default')).toContainText('Standard: Apple TV');
		await expect(page.getByTestId('profile-list').getByTestId('profile-badge-child')).toHaveCount(1);
		await expect(page.locator('[data-eq="suggestions"]')).toHaveCount(14);
		await expectEqualSizes(page.locator('[data-eq="suggestions"]'));
		await expect(page.getByTestId('suggestion-adult').getByTestId('sbadge-exists')).toBeVisible();
		await expect(page.getByTestId('suggestion-teen').getByTestId('sbadge-exists')).toHaveCount(0);
		await expectEqualSizes(page.getByTestId('gallery-filter').getByRole('radio'));
		await page.getByTestId('gallery-filter').getByRole('radio', { name: 'Kinder' }).click();
		await expect(page.locator('[data-eq="suggestions"]')).toHaveCount(5);
		expect(errors).toEqual([]);
	});

	test('adding a suggestion creates a copy and opens the editor', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		await page.getByTestId('suggestion-add-teen').click();
		await expect(page.getByTestId('profile-title')).toHaveText('Teen (13–17)');
		expect(writes(hub, 'POST', '/profile-suggestions/teen')).toHaveLength(1);
	});

	test('new version of a suggestion: banner, preview, take over', async ({ page }) => {
		const { hub } = await boot(page, { bumpSuggestion: { child: 9 } });
		await page.goto('/profiles');
		await expect(page.getByTestId('suggestion-child').getByTestId('sbadge-update')).toContainText('Neue Version 9');
		await expect(page.getByTestId('suggestion-teen').getByTestId('sbadge-starter')).toHaveCount(0);
		await page.getByTestId('suggestion-update-child').click();
		await expect(page.getByTestId('update-dialog')).toBeVisible();
		await expect(page.getByTestId('update-preview-count')).toContainText('1 Gerät');
		await page.getByTestId('apply-update').click();
		await expect(page.getByTestId('suggestion-update')).toHaveCount(0);
		expect(writes(hub, 'POST', '/profiles/p-child/apply-suggestion').map((r) => r[1])).toEqual(['/profiles/p-child/apply-suggestion?dry_run=true', '/profiles/p-child/apply-suggestion']);
	});

	test('new profile: three equal tiles, empty profile with parental controls', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		await page.getByTestId('new-profile').click();
		const tiles = page.locator('[data-eq="new-profile"] > button');
		await expect(tiles).toHaveCount(3);
		await expectEqualSizes(tiles);
		await page.getByTestId('new-empty').click();
		await page.getByTestId('new-name').fill('Grandparent');
		await page.getByTestId('new-kind').selectOption('child');
		await page.getByTestId('new-child').click();
		await page.getByTestId('new-create').click();
		await expect(page.getByTestId('profile-title')).toHaveText('Grandparent');
		// Parental controls start on "Hub only".
		expect(writes(hub, 'POST', '/profiles')[0][2]).toEqual({ name: 'Grandparent', kind: 'child', child_protection: true, rules: { control_path: 'hub' } });
	});

	test('default profiles per device type and guest duration', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		await page.getByTestId('default-ipados').selectOption('p-adult');
		await expect(page.getByTestId('default-guest').locator('option[value="p-child"]')).toHaveJSProperty('disabled', true);
		await expectEqualSizes(page.locator('[data-eq="guest-hours"] > button'));
		await page.getByTestId('guest-hours-168').click();
		await page.getByTestId('default-profiles-save').click();
		const put = writes(hub, 'PUT', '/settings/profiles')[0][2];
		expect(put.default_profiles.ipados).toBe('p-adult');
		expect(put.guest_default_hours).toBe(168);
	});

	test('Android device types have their own default profile', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		const card = page.getByTestId('default-profiles');
		await expect(card.getByText('Android TV', { exact: true })).toBeVisible();
		await expect(page.getByTestId('default-android_tv')).toHaveValue('p-tv');
		await expect(page.getByTestId('default-android_tablet')).toHaveValue('p-wall');
		// No released Wear OS app: no slot, and its stored default is saved unchanged.
		await expect(page.getByTestId('default-wearos')).toHaveCount(0);
		await expect(card).not.toContainText('Wear OS');
		await expectEqualSizes(card.locator('.slot'));
		await page.getByTestId('default-android_tv').selectOption('p-adult');
		await page.getByTestId('default-profiles-save').click();
		const put = writes(hub, 'PUT', '/settings/profiles')[0][2];
		expect(put.default_profiles.android_tv).toBe('p-adult');
		expect(put.default_profiles.tvos).toBe('p-tv');
		expect(put.default_profiles.wearos).toBe('p-adult');
		expect(Object.keys(put.default_profiles).sort()).toEqual(
			['android', 'android_tablet', 'android_tv', 'guest', 'ios', 'ipados', 'tvos', 'watchos', 'wearos'],
		);
	});

	test('platforms without an app are listed nowhere', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		// The default badge of Adult (iPhone, Apple Watch, Android phone, Wear OS) names no Wear OS.
		await expect(page.getByTestId('profile-list')).toContainText('Apple Watch');
		await expect(page.getByTestId('profile-list')).not.toContainText('Wear OS');
		await page.goto('/clients');
		await page.getByTestId('open-pairing').first().click();
		await expect(page.getByRole('dialog')).not.toContainText('Wear OS');
		expect(hub).toBeTruthy();
	});

	test('a hidden platform follows its shown sibling when that one moves away', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		// Apple Watch and Wear OS both point to Adult; the Apple Watch moves to the wall panel profile.
		await page.getByTestId('default-watchos').selectOption('p-wall');
		await page.getByTestId('default-profiles-save').click();
		const put = writes(hub, 'PUT', '/settings/profiles')[0][2];
		expect(put.default_profiles.watchos).toBe('p-wall');
		expect(put.default_profiles.wearos).toBe('p-wall');
		expect(put.default_profiles.ios).toBe('p-adult');
	});

	test('rules name device classes, not single platforms', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-adult');
		await expect(page.getByText('Gilt für: Telefon, Tablet, Fernseher, Uhr').first()).toBeVisible();
		await expect(page.getByText('Gilt für: Tablet', { exact: true }).first()).toBeVisible();
	});
});

test.describe('Profiles – editor', () => {
	test('groups, “Fixed” per rule, enforcement and the alert note', async ({ page }) => {
		const { errors } = await boot(page);
		await page.goto('/profiles/p-adult');
		for (const g of ['access', 'remote', 'areas', 'features', 'home', 'display', 'panel', 'location']) await expect(page.getByTestId(`group-${g}`)).toBeVisible();
		await expect(page.getByTestId('kids-off')).toBeVisible();
		await expect(page.getByTestId('fixed-remote_access')).not.toBeChecked();
		// Adult is on "Checked": Home Assistant enforces operating.
		await expect(page.getByTestId('enf-areas')).toContainText('Bedienen durchgesetzt');
		await expect(page.getByTestId('enf-remote_access')).toContainText('Durchgesetzt');
		await expect(page.getByTestId('safety-note')).toContainText('Wichtige Meldungen (Alarm, Rauch, Wasser) blendet kein Profil aus.');
		await expect(page.getByTestId('safety-note')).toContainText(`${BRAND.name} ersetzt keine Alarmanlage. Meldungen erscheinen, wenn die App geöffnet ist.`);
		await expect(page.getByTestId('p-notification_level-safety-notifications')).toContainText('Wichtige Meldungen (Alarm, Rauch, Wasser) blendet kein Profil aus.');
		await expect(page.locator('body')).not.toContainText('Kommen immer an');
		// When away: alarm and smoke at most “show only”
		const alarm = page.getByTestId('rc-type-alarm_control_panel');
		await expect(alarm.getByRole('radio', { name: 'Ausblenden' })).toBeDisabled();
		await expect(alarm.getByRole('radio', { name: 'Nur anzeigen' })).toBeEnabled();
		await expect(page.getByTestId('rc-type-lock').getByRole('radio', { name: 'Ausblenden' })).toBeEnabled();
		expect(errors).toEqual([]);
	});

	test('parental controls: boundaries as locked lines with “Why?”, kids rules enforced', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-child');
		// The child is on "Hub only" without remote access: widgets need the device's own key.
		const widgets = page.getByTestId('decided-quick_access');
		await expect(widgets).toContainText('Braucht einen eigenen Home-Assistant-Schlüssel');
		await widgets.getByRole('button', { name: 'Warum?' }).click();
		await expect(page.getByTestId('why-quick_access')).toContainText('Widgets, Siri und Kurzbefehle');
		await expect(page.getByTestId('group-kids')).toBeVisible();
		await expect(page.getByTestId('enf-areas')).toContainText('Durchgesetzt');
		await expect(page.getByTestId('always-fixed-kids_exceptions')).toBeVisible();
		await expect(page.getByTestId('safety-kids')).toContainText('Wichtige Meldungen');
	});

	test('summary sentence names the bedtime once (days, start)', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-child');
		const sum = page.getByTestId('profile-summary');
		await expect(sum).toContainText('Schlafenszeit Mo–Fr ab 20:00');
		await expect(sum).not.toContainText('20:00 ab');
	});

	test('saving always shows the preview; loosening needs a confirmation', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles/p-guest');
		await page.getByTestId('p-remote_access-remote_access').getByRole('radio', { name: 'An' }).click();
		await expect(page.getByTestId('save-bar')).toContainText('Betrifft bis zu 1 Gerät');
		await page.getByTestId('save-preview').click();
		await expect(page.getByTestId('save-preview-count')).toContainText('Diese Änderung betrifft 1 Gerät');
		await expect(page.getByTestId('save-preview-device')).toContainText('iPhone Visitor');
		await expect(page.getByTestId('save-preview-device')).toContainText('Kann dann auch von unterwegs steuern');
		await expect(page.getByTestId('save-confirm')).toBeDisabled();
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('save-confirm').click();
		await expect(page.getByTestId('save-bar')).toHaveCount(0);
		const patches = writes(hub, 'PATCH', '/profiles/p-guest');
		expect(patches.map((r) => r[1])).toEqual(['/profiles/p-guest?dry_run=true', '/profiles/p-guest']);
		expect(patches[1][2]).toEqual({ rules: { remote_access: true } });
	});

	test('making a rule fixed drops adjustments (warning + preview)', async ({ page }) => {
		const { hub } = await boot(page, { overrides: { 'c-owner-a': [{ rule: 'camera_live', value: false, user_choice: false, expires_at: null, note: null, created_by: 'Owner A', created_at: '2026-11-18T10:00:00Z' }] } });
		await page.goto('/profiles/p-adult');
		await page.getByTestId('fixed-camera_live').check();
		await expect(page.getByTestId('fixed-drops-camera_live')).toBeVisible();
		await page.getByTestId('save-preview').click();
		await expect(page.getByTestId('save-preview-removed')).toContainText('Kamera-Livebild');
		// The device had camera off; fixed, the profile value (on) applies: a loosening.
		await expect(page.getByTestId('save-confirm')).toBeDisabled();
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('save-confirm').click();
		await expect(page.getByTestId('save-bar')).toHaveCount(0);
		expect(writes(hub, 'PATCH', '/profiles/p-adult')[1][2].fixed).toContain('camera_live');
	});

	test('profile remote PIN: entered twice, never shown, fixed checkbox', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles/p-wall');
		await page.getByTestId('profile-pin-set').click();
		await page.getByTestId('profile-pin-pin1').fill('12345678');
		await page.getByTestId('profile-pin-pin2').fill('12345679');
		await page.getByTestId('profile-pin-save').click();
		await expect(page.getByTestId('profile-pin')).toContainText('nicht gleich');
		await page.getByTestId('profile-pin-pin2').fill('12345678');
		await page.getByTestId('profile-pin-save').click();
		await expect(page.getByTestId('profile-pin-state')).toContainText('Gesetzt');
		await expect(page.locator('body')).not.toContainText('12345678');
		await page.getByTestId('profile-pin-fixed').check();
		await expect(page.getByTestId('profile-pin-fixed')).toBeChecked();
		const patches = writes(hub, 'PATCH', '/profiles/p-wall');
		expect(patches.map((r) => r[2])).toEqual([{ remote_pin: '********' }, { remote_pin_fixed: true }]);
	});

	test('delete: replacement profile required, default profiles protected', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles/p-adult');
		await page.getByTestId('profile-delete').click();
		await expect(page.getByTestId('delete-default')).toContainText('Standardprofil für iPhone');
		await page.keyboard.press('Escape');
		await page.goto('/profiles/p-child');
		await page.getByTestId('profile-delete').click();
		await expect(page.getByTestId('delete-preview-btn')).toBeDisabled();
		await page.getByTestId('delete-replacement').selectOption('p-adult');
		await page.getByTestId('delete-preview-btn').click();
		await expect(page.getByTestId('delete-preview-device')).toContainText('Kids iPad');
		await expect(page.getByTestId('delete-preview-device')).toContainText('Bekommt einen eigenen Home-Assistant-Schlüssel');
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('delete-confirm').click();
		await expect(page).toHaveURL(/\/profiles$/);
		expect(writes(hub, 'DELETE', '/profiles/p-child').map((r) => r[1])).toEqual(['/profiles/p-child?replacement=p-adult&dry_run=true', '/profiles/p-child?replacement=p-adult']);
	});
});

test.describe('Device page', () => {
	test('effective values with origin, adjust for 7 days, reset', async ({ page }) => {
		const { hub, errors } = await boot(page);
		await page.goto('/clients/c-kid');
		await expect(page.getByTestId('device-profile-chip')).toContainText('Kind');
		await expect(page.getByTestId('src-quick_access')).toContainText('Braucht einen eigenen Home-Assistant-Schlüssel');
		await expect(page.getByTestId('src-kids_quiet_hours')).toContainText('Vom Profil');
		await expect(page.getByTestId('src-kids_blocked_types')).toContainText('Fest im Profil');
		await expect(page.getByTestId('inprofile-kids_blocked_types')).toBeVisible();
		await page.getByTestId('adjust-control_mode_max').click();
		const dlg = page.getByTestId('adjust-dialog');
		await expect(dlg).toContainText('Nur für Kids iPad');
		await expectEqualSizes(dlg.locator('[data-eq="durations"] > label'));
		await dlg.getByRole('radio', { name: 'Einfach' }).click();
		await page.getByTestId('duration-7d').check();
		await expect(page.getByTestId('adjust-until')).toContainText('Danach wieder der Profilwert (Erweitert)');
		await page.getByTestId('adjust-note').fill('Ferien');
		await page.getByTestId('adjust-save').click();
		await expect(page.getByTestId('src-control_mode_max')).toContainText('Angepasst bis');
		await expect(page.getByTestId('profile-history')).toContainText('Bedienung höchstens: Erweitert → Einfach');
		await expect(page.getByTestId('profile-history')).toContainText('„Ferien“');
		expect(writes(hub, 'PUT', '/pairing/clients/c-kid/overrides/control_mode_max')[0][2]).toEqual({ value: 'basic', note: 'Ferien', expires: '7d' });
		await page.getByTestId('rules-filter').getByRole('radio', { name: 'Angepasst' }).click();
		await expect(page.locator('[data-testid^="eff-"]')).toHaveCount(1);
		await page.getByTestId('reset-control_mode_max').click();
		await expect(page.getByTestId('rules-empty')).toBeVisible();
		// A reset checks the preview first (dry run), then resets.
		expect(writes(hub, 'DELETE', '/pairing/clients/c-kid/overrides/control_mode_max').map((r) => r[1])).toEqual(['/pairing/clients/c-kid/overrides/control_mode_max?dry_run=true', '/pairing/clients/c-kid/overrides/control_mode_max']);
		expect(errors).toEqual([]);
	});

	test('guest: loosening a security rule asks first, durations incl. date and permanent', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/clients/c-visitor');
		await expect(page.getByTestId('device-access')).toContainText('Gast bis');
		// The guest profile fixes "Hub only".
		await expect(page.getByTestId('eff-control_path')).toContainText('Nur über den Hub');
		await expect(page.getByTestId('src-control_path')).toContainText('Fest im Profil');
		await page.getByTestId('adjust-remote_access').click();
		await expect(page.getByTestId('adjust-remote-key-warning')).toHaveCount(0);
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'An' }).click();
		await expect(page.getByTestId('adjust-remote-key-warning')).toContainText('jeden Zustand in Home Assistant lesen');
		await expect(page.getByTestId('loosening-warning')).toContainText('von überall');
		await expect(page.getByTestId('adjust-save')).toBeDisabled();
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('duration-tomorrow_morning').check();
		await page.getByTestId('adjust-save').click();
		await expect(page.getByTestId('src-remote_access')).toContainText('Angepasst bis');
		expect(writes(hub, 'PUT', '/pairing/clients/c-visitor/overrides/remote_access').filter((r) => !r[1].includes('dry_run'))[0][2]).toEqual({ value: true, note: null, expires: 'tomorrow_morning' });
	});

	test('choose profile with preview, own area, profile from device', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/clients/c-owner-b');
		await page.getByTestId('device-choose-profile').click();
		await page.getByTestId('profile-picker-p-child').check();
		await page.getByTestId('assign-preview-btn').click();
		await expect(page.getByTestId('assign-preview-device')).toContainText('Verliert seinen Home-Assistant-Schlüssel');
		await page.getByTestId('assign-save').click();
		await expect(page.getByTestId('device-profile-chip')).toContainText('Kind');
		expect(writes(hub, 'PUT', '/pairing/clients/c-owner-b/profile').map((r) => r[2])).toEqual([
			{ profile_id: 'p-child', discard_overrides: false },
			{ profile_id: 'p-child', discard_overrides: false },
		]);
		await page.getByTestId('own-area').selectOption('a-buero');
		await expect.poll(() => writes(hub, 'PUT', '/pairing/clients/c-owner-b/own-area').length).toBe(1);
		await page.getByTestId('profile-from-device').click();
		await page.getByTestId('new-name').fill('Wie Owner B');
		await page.getByTestId('new-create').click();
		await expect(page.getByTestId('device-profile-chip')).toContainText('Wie Owner B');
		expect(writes(hub, 'POST', '/profiles/from-device')[0][2]).toEqual({ client_id: 'c-owner-b', name: 'Wie Owner B', assign: true });
	});

	test('parent approval: 15 min … “Until tomorrow morning” as equal buttons', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/clients/c-kid');
		await expect(page.getByTestId('kids-status')).toBeVisible();
		await expectEqualSizes(page.locator('[data-eq="release"] .btn'));
		await page.getByTestId('kids-release-morning').click();
		await expect(page.getByTestId('kids-released')).toBeVisible();
		expect(writes(hub, 'POST', '/pairing/clients/c-kid/kids-override').map((r) => r[2])).toEqual([{ until: 'bedtime_end' }]);
		await page.getByTestId('kids-release-30').click();
		await expect.poll(() => writes(hub, 'POST', '/pairing/clients/c-kid/kids-override').length).toBe(2);
		expect(writes(hub, 'POST', '/pairing/clients/c-kid/kids-override')[1][2]).toEqual({ minutes: 30 });
	});

	test('remote PIN: from the profile, own PIN unless the profile fixes it', async ({ page }) => {
		const { hub } = await boot(page, {
			extra: (s) => {
				const p = s.profiles.find((x) => x.id === 'p-wall');
				p.has_remote_pin = true;
			},
		});
		await page.goto('/clients/c-tv');
		await expect(page.getByTestId('device-pin-state')).toContainText('Keine PIN');
		await page.goto('/clients/c-panel');
		await expect(page.getByTestId('device-pin-state')).toContainText('Eigene PIN');
		await page.getByTestId('device-pin-remove').click();
		await expect(page.getByTestId('device-pin-state')).toContainText('PIN vom Profil');
		await page.getByTestId('device-pin-set').click();
		await page.getByTestId('device-pin-dialog-pin1').fill('87654321');
		await page.getByTestId('device-pin-dialog-pin2').fill('87654321');
		await page.getByTestId('device-pin-dialog-save').click();
		await expect(page.getByTestId('device-pin-state')).toContainText('Eigene PIN');
		expect(writes(hub, 'PUT', '/pairing/clients/c-panel/remote-pin').map((r) => r[2])).toEqual([{ pin: null }, { pin: '********' }]);
	});

	test('remote PIN fixed in the profile: no own PIN on the device', async ({ page }) => {
		await boot(page, {
			extra: (s) => {
				const p = s.profiles.find((x) => x.id === 'p-wall');
				p.has_remote_pin = true;
				p.remote_pin_fixed = true;
			},
		});
		await page.goto('/clients/c-panel');
		await expect(page.getByTestId('device-pin-state')).toContainText('PIN vom Profil');
		await expect(page.getByTestId('device-pin')).toContainText('Das Profil legt die PIN fest');
		await expect(page.getByTestId('device-pin-set')).toBeDisabled();
	});
});

test.describe('Devices list, bulk, pairing', () => {
	test('several devices: assign a profile with preview', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/clients');
		await expect(page.getByTestId('device-table')).toContainText('Erwachsene Person');
		await page.getByTestId('select-c-owner-b').check();
		await page.getByTestId('select-c-visitor').check();
		await page.getByTestId('bulk-open').click();
		const dlg = page.getByTestId('bulk-dialog');
		await expectEqualSizes(page.getByTestId('bulk-action').getByRole('radio'));
		await page.getByTestId('bulk-profile-p-wall').check();
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-kpi-changed')).toHaveText('2');
		await expect(page.getByTestId('bulk-preview-detail-count')).toContainText('2 Geräte');
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('bulk-apply').click();
		await expect(page.getByTestId('bulk-done')).toBeVisible();
		const bulks = writes(hub, 'POST', '/pairing/clients/bulk').map((r) => r[2]);
		expect(bulks[1]).toEqual({ client_ids: ['c-owner-b', 'c-visitor'], action: 'assign_profile', dry_run: false, profile_id: 'p-wall', discard_overrides: false });
	});

	test('bulk: a guest is skipped for a profile with parental controls', async ({ page }) => {
		await boot(page);
		await page.goto('/clients');
		await page.getByTestId('select-c-owner-b').check();
		await page.getByTestId('select-c-visitor').check();
		await page.getByTestId('bulk-open').click();
		await expect(page.getByTestId('bulk-profile-p-child')).toBeDisabled();
	});

	test('pairing a member: default per device type preselected', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/clients');
		await page.getByTestId('open-pairing').click();
		await expect(page.getByTestId('pairing-profile-auto')).toBeChecked();
		await expect(page.getByTestId('pairing-profile')).toContainText('iPad, Android-Tablet → Wandpanel');
		await expectEqualSizes(page.locator('[data-eq="profile-tiles"] > label'));
		await page.getByTestId('pairing-generate').click();
		await expect(page.getByTestId('pairing-pin')).toHaveText('48213907');
		expect(writes(hub, 'POST', '/pairing/generate')[0][2]).toEqual({ access: 'member' });
	});

	test('guest invitation: guest profile, no parental controls, duration up to 90 days', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/guests');
		await page.getByTestId('open-pairing-guest').click();
		await expect(page.getByTestId('pairing-profile-p-guest')).toBeChecked();
		await expect(page.getByTestId('pairing-profile-p-child')).toBeDisabled();
		await expectEqualSizes(page.locator('[data-eq="guest-duration"] > button'));
		await page.getByTestId('pairing-days-3').click();
		await page.getByTestId('pairing-own-area').selectOption('a-gast');
		await expect(page.getByTestId('pairing-remote')).toContainText('nur zu Hause laut Profil „Gast“');
		await page.getByTestId('pairing-generate').click();
		await expect(page.getByTestId('pairing-pin')).toBeVisible();
		expect(writes(hub, 'POST', '/pairing/generate')[0][2]).toEqual({ access: 'guest', profile_id: 'p-guest', own_area_id: 'a-gast', guest_hours: 72 });
	});

	test('guests page shows profile and remote access per guest', async ({ page }) => {
		await boot(page);
		await page.goto('/guests');
		await expect(page.getByTestId('guest-card')).toHaveCount(1);
		await expect(page.getByTestId('guest-profile')).toContainText('Gast');
		await expect(page.getByTestId('guest-remote')).toContainText('Nur zu Hause');
		await expect(page.getByTestId('default-profiles')).toBeVisible();
		await expectEqualSizes(page.locator('[data-eq="guest-actions"] > .btn'));
		await page.getByTestId('guest-open').click();
		await expect(page).toHaveURL(/\/clients\/c-visitor$/);
	});
});

test.describe('Accessibility', () => {
	async function unnamed(page) {
		return page.evaluate(() => {
			const out = [];
			for (const el of document.querySelectorAll('button, input, select, textarea, a[href], [role="radio"], [role="switch"]')) {
				const r = el.getBoundingClientRect();
				if (el.type === 'hidden' || (r.width === 0 && r.height === 0 && el.type !== 'radio' && el.type !== 'checkbox')) continue;
				const label = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title') || (el.labels && [...el.labels].map((l) => l.textContent).join('')) || el.textContent;
				if (!String(label || '').trim()) out.push(el.outerHTML.slice(0, 120));
			}
			return out;
		});
	}

	test('every control on editor, device page and dialogs has a name', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-child');
		await expect(page.getByTestId('group-kids')).toBeVisible();
		expect(await unnamed(page)).toEqual([]);
		await page.goto('/clients/c-kid');
		await expect(page.getByTestId('effective-rules')).toBeVisible();
		expect(await unnamed(page)).toEqual([]);
		await page.getByTestId('adjust-control_mode_max').click();
		expect(await unnamed(page)).toEqual([]);
	});

	test('keyboard: Enter opens “Adjust”, Esc closes, focus returns', async ({ page }) => {
		await boot(page);
		await page.goto('/clients/c-kid');
		const btn = page.getByTestId('adjust-control_mode_max');
		await btn.focus();
		await page.keyboard.press('Enter');
		await expect(page.getByTestId('adjust-dialog')).toBeVisible();
		await expect(page.getByTestId('adjust-dialog').locator(':focus')).toHaveCount(1);
		await page.keyboard.press('Escape');
		await expect(page.getByTestId('adjust-dialog')).toHaveCount(0);
		await expect(btn).toBeFocused();
	});
});

test.describe('Phone and English', () => {
	test.use({ viewport: { width: 390, height: 844 } });

	test('no horizontal scrolling on the profile screens', async ({ page }) => {
		await boot(page);
		for (const url of ['/profiles', '/profiles/p-child', '/profiles/p-adult', '/clients/c-kid', '/clients/c-visitor', '/guests']) {
			await page.goto(url);
			await page.waitForLoadState('networkidle');
			await noHorizontalScroll(page);
		}
		await page.goto('/clients/c-kid');
		await page.getByTestId('adjust-control_mode_max').click();
		await expectEqualSizes(page.locator('[data-eq="durations"] > label'));
		await noHorizontalScroll(page);
	});

	test('English texts', async ({ page }) => {
		await boot(page, {}, 'en');
		await page.goto('/profiles/p-adult');
		await expect(page.getByTestId('fixed-remote_access').locator('..')).toContainText('Fixed');
		await expect(page.getByTestId('safety-note')).toContainText('No profile hides important alerts (alarm, smoke, water).');
		await expect(page.getByTestId('safety-note')).toContainText(`${BRAND.name} is not an alarm system; alerts appear while the app is open.`);
		await expect(page.getByTestId('enf-areas')).toContainText('Control enforced');
		await page.goto('/clients/c-kid');
		await expect(page.getByTestId('src-quick_access')).toContainText('Needs the device\'s own Home Assistant key');
	});
});
