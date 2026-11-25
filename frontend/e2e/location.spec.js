import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * Location in the admin UI: the rules “Share location” and “Location
 * required” in the profile editor (only person and child), and what the
 * device shares on the device page.
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

const withLocation = (shared, at = null) => (state) => {
	for (const c of state.clients) {
		if (c.id === 'c-owner-a') Object.assign(c, { location_shared: shared, location_at: at });
		if (c.id === 'c-owner-b') Object.assign(c, { location_shared: 'background', location_at: new Date(Date.now() - 5 * 60_000).toISOString() });
	}
};

test('profile editor: “Location required” next to “Share location”, hint while location is off', async ({ page }) => {
	const { errors } = await boot(page);
	await page.goto('/profiles/p-adult');
	const rule = page.getByTestId('rule-location_required');
	await expect(rule).toContainText('Standort erforderlich');
	await expect(rule.getByTestId('location-needs-sharing')).toHaveCount(0);
	await page.getByTestId('p-location-location').getByRole('radio', { name: 'Aus' }).click();
	await expect(rule.getByTestId('location-needs-sharing')).toBeVisible();
	expect(errors).toEqual([]);
});

test('profile editor: guests are never asked for their location', async ({ page }) => {
	const { errors } = await boot(page);
	await page.goto('/profiles/p-guest');
	await expect(page.getByTestId('kind-only-location')).toBeVisible();
	await expect(page.getByTestId('kind-only-location_required')).toBeVisible();
	expect(errors).toEqual([]);
});

test('device page: not shared although required, shared in the background', async ({ page }) => {
	const { errors } = await boot(page, {
		overrides: { 'c-owner-a': [{ rule: 'location_required', value: true, expires_at: null, user_choice: false }] },
		extra: withLocation('none'),
	});
	await page.goto('/clients/c-owner-a');
	const fact = page.getByTestId('fact-location');
	await expect(fact).toContainText('Nicht geteilt · erforderlich');
	await expect(fact.locator('.badge')).toHaveClass(/badge-warn/);
	await page.goto('/clients/c-owner-b');
	await expect(page.getByTestId('fact-location')).toContainText('Im Hintergrund geteilt');
	await expect(page.getByTestId('fact-location')).toContainText('Zuletzt');
	expect(errors).toEqual([]);
});

test('device page: unknown status (EN), no location row for the guest and the panel', async ({ page }) => {
	const { errors } = await boot(page, { extra: withLocation(null) }, 'en');
	await page.goto('/clients/c-owner-a');
	await expect(page.getByTestId('fact-location')).toContainText('Not reported yet');
	await page.goto('/clients/c-visitor');
	await expect(page.getByTestId('device-profile')).toBeVisible();
	await expect(page.getByTestId('fact-location')).toHaveCount(0);
	await page.goto('/clients/c-panel');
	await expect(page.getByTestId('device-profile')).toBeVisible();
	await expect(page.getByTestId('fact-location')).toHaveCount(0);
	expect(errors).toEqual([]);
});
