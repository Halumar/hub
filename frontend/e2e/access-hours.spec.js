import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * Access hours, access switch and babysitter in the admin UI: the rule
 * editor (week windows like bedtime), “Access now” with the switch on the
 * device page, the babysitter rule only for kind babysitter.
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

const writes = (hub, method, prefix) => hub.requests.filter(([m, p]) => m === method && p.startsWith(prefix));

async function saveProfile(page) {
	await page.getByTestId('save-preview').click();
	const confirm = page.getByTestId('loosening-confirm');
	if (await confirm.isVisible().catch(() => false)) await confirm.check();
	await page.getByTestId('save-confirm').click();
	await expect(page.getByTestId('save-bar')).toHaveCount(0);
}

test('rule “Access hours”: switched on with a window, saved as object', async ({ page }) => {
	const { hub, errors } = await boot(page);
	await page.goto('/profiles/p-guest');
	const rule = page.getByTestId('rule-access_hours');
	await expect(rule).toContainText('Zugangszeiten');
	await rule.getByTestId('p-access_hours-access-on').click();
	await expect(rule.getByTestId('quiet-window')).toHaveCount(1);
	await rule.getByTestId('day-sat').click();
	await rule.getByTestId('quiet-start').fill('17:00');
	await rule.getByTestId('quiet-end').fill('01:00');
	await saveProfile(page);
	const patch = writes(hub, 'PATCH', '/profiles/p-guest').at(-1)[2];
	expect(patch.rules.access_hours.enabled).toBe(true);
	expect(patch.rules.access_hours.windows).toEqual([{ days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sun'], start: '17:00', end: '01:00' }]);
	expect(errors).toEqual([]);
});

test('device page: access now with the switch (guest), none for an adult', async ({ page }) => {
	const { hub, errors } = await boot(page);
	await page.goto('/clients/c-visitor');
	const card = page.getByTestId('device-access-now');
	await expect(card).toBeVisible();
	await expect(page.getByTestId('device-access-state')).toHaveText('Offen');
	await page.getByTestId('device-access-switch').click();
	await expect(page.getByTestId('device-access-state')).toHaveText('Zu – Schalter aus');
	expect(writes(hub, 'PUT', '/pairing/clients/c-visitor/access-switch').at(-1)[2]).toEqual({ on: false });
	await page.goto('/clients/c-owner-a');
	await expect(page.getByTestId('device-profile')).toBeVisible();
	await expect(page.getByTestId('device-access-now')).toHaveCount(0);
	expect(errors).toEqual([]);
});

test('device page: closed by schedule shows when access opens again (EN)', async ({ page }) => {
	const opens = new Date(Date.now() + 26 * 3600 * 1000).toISOString();
	const { errors } = await boot(
		page,
		{
			overrides: {
				'c-visitor': [{ rule: 'access_hours', value: { enabled: true, windows: [{ days: ['mon'], start: '18:00', end: '23:30' }] }, expires_at: null, user_choice: false }],
			},
			extra: (state) => {
				const visitor = state.clients.find((c) => c.id === 'c-visitor');
				visitor.access_open = false;
				visitor.access_opens_at = opens;
			},
		},
		'en',
	);
	await page.goto('/clients/c-visitor');
	await expect(page.getByTestId('device-access-state')).toContainText('Closed until');
	expect(errors).toEqual([]);
});

test('babysitter rule only for kind babysitter', async ({ page }) => {
	await boot(page);
	await page.goto('/profiles/p-adult');
	await expect(page.getByTestId('kind-only-babysitter_requests')).toBeVisible();
	await page.getByTestId('profile-kind').selectOption('babysitter');
	await expect(page.getByTestId('kind-only-babysitter_requests')).toHaveCount(0);
});
