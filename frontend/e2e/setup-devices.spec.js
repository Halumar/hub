import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

// The dev server compiles the profile pages on first use; give the first look more time.
test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * “Set up new devices” in the admin UI: profile kind, the rule
 * setup_devices (on/off, kinds, only for kind person), “Set up by” on the
 * device page, kind badge on the profile cards.
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

async function expectEqualSizes(locator) {
	const boxes = await locator.evaluateAll((els) => els.filter((e) => e.offsetParent !== null).map((e) => e.getBoundingClientRect()).map((r) => ({ w: r.width, h: r.height })));
	expect(boxes.length).toBeGreaterThan(1);
	for (const b of boxes) {
		expect(Math.abs(b.w - boxes[0].w)).toBeLessThanOrEqual(1.5);
		expect(Math.abs(b.h - boxes[0].h)).toBeLessThanOrEqual(1.5);
	}
}

const writes = (hub, method, prefix) => hub.requests.filter(([m, p]) => m === method && p.startsWith(prefix));

async function saveProfile(page) {
	await page.getByTestId('save-preview').click();
	const confirm = page.getByTestId('loosening-confirm');
	if (await confirm.isVisible().catch(() => false)) await confirm.check();
	await page.getByTestId('save-confirm').click();
	await expect(page.getByTestId('save-bar')).toHaveCount(0);
}

test('rule “Set up new devices”: on, kinds as equal chips, saved as object', async ({ page }) => {
	const { hub, errors } = await boot(page);
	await page.goto('/profiles/p-adult');
	const rule = page.getByTestId('rule-setup_devices');
	await expect(rule).toContainText('Neue Geräte einrichten');
	await rule.getByTestId('p-setup_devices-setup-on').click();
	const chips = rule.locator('[data-testid^="p-setup_devices-setup-kind-"]');
	await expect(chips).toHaveCount(7); // incl. babysitter
	await expectEqualSizes(chips);
	await expect(rule.getByTestId('p-setup_devices-setup-kind-guest')).toHaveAttribute('aria-pressed', 'true');
	await rule.getByTestId('p-setup_devices-setup-kind-child').click();
	await rule.getByTestId('p-setup_devices-setup-kind-panel').click();
	await saveProfile(page);
	const patch = writes(hub, 'PATCH', '/profiles/p-adult').at(-1)[2];
	expect(patch).toEqual({ rules: { setup_devices: { enabled: true, kinds: ['guest', 'child', 'panel'] } } });
	expect(errors).toEqual([]);
});

test('profile kind: chosen in the editor; other kinds show “only for kind person”', async ({ page }) => {
	const { hub } = await boot(page);
	await page.goto('/profiles/p-adult');
	await expect(page.getByTestId('profile-kind')).toHaveValue('person');
	await expect(page.getByTestId('kind-only-setup_devices')).toHaveCount(0);
	await page.getByTestId('profile-kind').selectOption('panel');
	await expect(page.getByTestId('kind-only-setup_devices')).toContainText('Person');
	await saveProfile(page);
	expect(writes(hub, 'PATCH', '/profiles/p-adult').at(-1)[2]).toEqual({ kind: 'panel' });
	await page.goto('/profiles/p-child');
	await expect(page.getByTestId('profile-kind')).toHaveValue('child');
	await expect(page.getByTestId('kind-only-setup_devices')).toBeVisible();
});

test('profile cards show the kind', async ({ page }) => {
	await boot(page);
	await page.goto('/profiles');
	const cards = page.getByTestId('profile-card');
	await expect(cards).toHaveCount(5);
	await expect(cards.filter({ hasText: 'Wandpanel' }).getByTestId('profile-badge-kind')).toContainText('Wandpanel');
	await expect(cards.filter({ hasText: 'Erwachsene Person' }).getByTestId('profile-badge-kind')).toContainText('Person');
});

test('device page names who set the device up', async ({ page }) => {
	await boot(page, {}, 'en');
	await page.goto('/clients/c-visitor');
	await expect(page.getByTestId('device-set-up-by')).toContainText('Invited by iPhone Owner B');
	await page.goto('/clients/c-owner-a');
	await expect(page.getByTestId('device-set-up-by')).toHaveCount(0);
});
