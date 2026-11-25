import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, defaultClients, AREAS } from './profiles-hub.js';

// The dev server compiles the pages on first use; give the first look more time.
test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * A child's Apple Watch: the kids rule “Allow a
 * watch” in the profile editor, and the watch listed under its phone with the
 * kids badge (the hub answers it with the phone's profile).
 */

function withChildWatch() {
	const clients = defaultClients();
	const base = clients.find((c) => c.id === 'c-owner-a');
	clients.push(
		{ ...base, id: 'c-kid-phone', name: 'iPhone Kid', owner_label: 'Kid', profile_id: 'p-child', created_at: base.created_at },
		{
			...base,
			id: 'c-kid-watch',
			name: 'Apple Watch Kid',
			owner_label: null,
			platform: 'watchos',
			device_model: 'Watch7,1',
			profile_id: 'p-child',
			is_connected: false,
			set_up_by: { client_id: 'c-kid-phone', name: 'iPhone Kid', method: 'onboard' },
		},
	);
	return clients;
}

async function boot(page, opts = {}, lang = 'de') {
	const hub = createProfilesHub(opts);
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
	await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
	return { hub, errors };
}

test('a child profile offers “Allow a watch”; the watch stays locked until it is on with remote access', async ({ page }) => {
	const { errors } = await boot(page);
	await page.goto('/profiles/p-child');
	const rule = page.getByTestId('rule-kids_watch');
	await expect(rule).toContainText('Uhr erlauben');
	await expect(page.getByTestId('kids-watch-needs-remote')).toContainText('Von unterwegs steuern');
	await expect(page.getByTestId('decided-watch')).toContainText('Uhr erlauben');
	// Remote access on, then "Allow a watch": the watch rule becomes editable.
	await page.getByTestId('p-remote_access-remote_access').getByRole('radio', { name: 'An' }).click();
	await page.getByTestId('p-kids_watch-kids_watch').getByRole('radio', { name: 'An' }).click();
	await expect(page.getByTestId('decided-watch')).toHaveCount(0);
	expect(errors).toEqual([]);
});

test('an adult profile has no “Allow a watch”', async ({ page }) => {
	await boot(page, {}, 'en');
	await page.goto('/profiles/p-adult');
	await expect(page.getByTestId('rule-watch')).toBeVisible();
	await expect(page.getByTestId('rule-kids_watch')).toHaveCount(0);
});

for (const width of [1280, 390]) {
	test(`a child's watch is listed under its phone with the kids badge (${width}px)`, async ({ page }) => {
		await page.setViewportSize({ width, height: 900 });
		const { errors } = await boot(page, { clients: withChildWatch() });
		await page.goto('/clients');
		const note = page.getByTestId('watch-of-c-kid-watch');
		await expect(note).toContainText('Uhr von iPhone Kid');
		const names = await page.locator('a.dname').allTextContents();
		const phone = names.indexOf('iPhone Kid');
		expect(phone).toBeGreaterThanOrEqual(0);
		expect(names[phone + 1]).toBe('Apple Watch Kid');
		const watchRow = width > 700 ? page.getByTestId('device-table').locator('tr', { has: note }) : page.locator('article', { has: note });
		await expect(watchRow.getByTestId('profile-badge')).toHaveClass(/badge-accent/);
		expect(errors).toEqual([]);
	});
}

test("a child's watch page points to its phone instead of offering releases", async ({ page }) => {
	const { errors } = await boot(page, { clients: withChildWatch() });
	await page.goto('/clients/c-kid-watch');
	await expect(page.getByTestId('kids-watch-follows')).toContainText('iPhone Kid');
	await expect(page.getByTestId('kids-watch-phone')).toHaveAttribute('href', /\/clients\/c-kid-phone$/);
	await expect(page.getByTestId('kids-release-15')).toHaveCount(0);
	await expect(page.getByTestId('kids-lift')).toHaveCount(0);
	expect(errors).toEqual([]);
});

test('the phone itself keeps its release buttons', async ({ page }) => {
	await boot(page, { clients: withChildWatch() });
	await page.goto('/clients/c-kid-phone');
	await expect(page.getByTestId('kids-status')).toBeVisible();
	await expect(page.getByTestId('kids-watch-follows')).toHaveCount(0);
});
