import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

/**
 * Effective rules of a device: the action buttons ("Profilwert verwenden",
 * "Anpassen", "Im Profil ändern") are never cut, at every width and in both languages, and are
 * equal in a list.
 */
test.use({ timezoneId: 'Europe/Zurich' });

const overrides = () => ({
	'c-owner-a': [
		{ rule: 'camera_live', value: false, user_choice: false, expires_at: null, note: null, created_by: 'Owner A', created_at: new Date().toISOString() },
		{ rule: 'remote_access', value: false, user_choice: false, expires_at: null, note: null, created_by: 'Owner A', created_at: new Date().toISOString() },
	],
});

const WIDTHS = [1440, 1280, 1101, 1024, 800, 390];

for (const lang of ['de', 'en']) {
	for (const width of WIDTHS) {
		test(`effective rules: action buttons not cut (${lang}, ${width} px)`, async ({ page }) => {
			const hub = createProfilesHub({ overrides: overrides() });
			await page.setViewportSize({ width, height: 900 });
			await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
			await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
			await page.goto('/clients/c-owner-a');
			const rules = page.getByTestId('effective-rules');
			await expect(rules).toBeVisible({ timeout: 15_000 });
			const buttons = rules.locator('.act > .btn');
			await expect(page.getByTestId('reset-camera_live')).toBeVisible();
			const cut = await buttons.evaluateAll((els) =>
				els
					.filter((e) => e.offsetParent !== null)
					.flatMap((e) => [...e.querySelectorAll('.truncate')])
					.filter((s) => s.scrollWidth > s.clientWidth + 0.5)
					.map((s) => s.textContent),
			);
			expect(cut).toEqual([]);
			const sizes = await buttons.evaluateAll((els) =>
				els.filter((e) => e.offsetParent !== null).map((e) => Math.round(e.getBoundingClientRect().width)),
			);
			expect(new Set(sizes).size).toBe(1);
		});
	}
}

/**
 * The editor names the suggestion a profile was copied from by its name, never by
 * its id (the contract's `origin` carries only `suggestion_id` and `version`).
 */
for (const lang of ['de', 'en']) {
	test(`profile editor: origin line names the suggestion (${lang})`, async ({ page }) => {
		const hub = createProfilesHub({
			extra: (state, { newProfile }) => state.profiles.push(newProfile('p-migrated', 'guest', 'Gast (2)')),
		});
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
		await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
		await page.goto('/profiles/p-migrated');
		await expect(page.getByTestId('profile-title')).toHaveText('Gast (2)', { timeout: 15_000 });
		const sub = page.locator('.t-sub').first();
		await expect(sub).toContainText(lang === 'de' ? 'Geräte' : 'device');
		await expect(sub).toContainText(lang === 'de' ? 'Aus Vorschlag „Gast“' : 'Guest');
		await expect(sub).not.toContainText('„guest“');
		await expect(sub).not.toContainText('"guest"');
	});
}
