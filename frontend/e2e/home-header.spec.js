import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

// The dev server compiles the profile pages on first use; give the first look more time.
test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * Rule home_header in the group "Home screen" of the
 * profile editor – two equal style cards with a preview image, three switches,
 * "User's choice" as the lock of the device setting.
 */

async function boot(page, lang = 'de') {
	const hub = createProfilesHub();
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
	await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
	return { hub, errors };
}

async function boxes(locator) {
	return locator.evaluateAll((els) => els.map((e) => e.getBoundingClientRect()).map((r) => ({ w: r.width, h: r.height })));
}

const writes = (hub, method, prefix) => hub.requests.filter(([m, p]) => m === method && p.startsWith(prefix));

for (const [lang, group, weather, compact, chips] of [
	['de', 'Startseite', 'Wetter-Stimmung', 'Kompakt', 'Status-Kapseln'],
	['en', 'Home screen', 'Weather mood', 'Compact', 'Status capsules'],
]) {
	test(`home screen section with equal style cards (${lang})`, async ({ page }) => {
		const { errors } = await boot(page, lang);
		await page.goto('/profiles/p-adult');
		const sec = page.getByTestId('group-home');
		await expect(sec.getByRole('heading', { name: group, exact: true })).toBeVisible();
		await expect(sec.getByTestId('rule-header')).toBeVisible();
		const row = sec.getByTestId('rule-home_header');
		const cards = row.getByRole('radio');
		await expect(cards).toHaveCount(2);
		await expect(cards.nth(0)).toContainText(weather);
		await expect(cards.nth(1)).toContainText(compact);
		await expect(cards.nth(0)).toHaveAttribute('aria-checked', 'true');
		// Equal sizes, also with the longer German texts.
		const b = await boxes(cards);
		expect(Math.abs(b[0].w - b[1].w)).toBeLessThanOrEqual(1.5);
		expect(Math.abs(b[0].h - b[1].h)).toBeLessThanOrEqual(1.5);
		// Preview images load.
		for (const img of await row.locator('img.hlight').all()) {
			await img.scrollIntoViewIfNeeded();
			await expect.poll(() => img.evaluate((i) => (i.complete ? i.naturalWidth : 0))).toBeGreaterThan(0);
		}
		await expect(row.getByRole('switch', { name: chips })).toBeVisible();
		await expect(row.getByRole('switch')).toHaveCount(3);
		await expect(row.getByTestId('uc-home_header')).toBeChecked();
		// No colored stripe at the left edge of the cards.
		const left = await cards.nth(0).evaluate((e) => getComputedStyle(e).borderLeftWidth === getComputedStyle(e).borderRightWidth);
		expect(left).toBe(true);
		expect(errors).toEqual([]);
	});
}

test('admin chooses compact without capsules and locks it on the devices', async ({ page }) => {
	const { hub } = await boot(page);
	await page.goto('/profiles/p-adult');
	const row = page.getByTestId('rule-home_header');
	await row.getByTestId('p-home_header-style-compact').click();
	await expect(row.getByTestId('p-home_header-style-compact')).toHaveAttribute('aria-checked', 'true');
	await row.getByTestId('p-home_header-status_chips').click();
	await row.getByTestId('uc-home_header').uncheck();
	await expect(page.getByTestId('status-home_header')).not.toContainText('Am Gerät wählbar');
	await page.getByTestId('save-preview').click();
	await page.getByTestId('save-confirm').click();
	await expect(page.getByTestId('save-bar')).toHaveCount(0);
	const body = writes(hub, 'PATCH', '/profiles/p-adult').at(-1)[2];
	expect(body.rules.home_header).toEqual({ style: 'compact', status_chips: false, greeting: true, weather: true });
	expect(body.user_choice).not.toContain('home_header');
	expect(body.user_choice).toContain('header');
});

test('header off: the head section says devices show no head', async ({ page }) => {
	await boot(page);
	await page.goto('/profiles/p-adult');
	await page.getByTestId('rule-header').getByRole('radio', { name: 'Aus' }).click();
	await expect(page.getByTestId('home-header-needs-header')).toBeVisible();
});

test('child profile starts compact without capsules', async ({ page }) => {
	await boot(page);
	await page.goto('/profiles/p-child');
	const row = page.getByTestId('rule-home_header');
	await expect(row.getByTestId('p-home_header-style-compact')).toHaveAttribute('aria-checked', 'true');
	await expect(row.getByTestId('p-home_header-status_chips')).toHaveAttribute('aria-checked', 'false');
	await expect(row.getByTestId('p-home_header-greeting')).toHaveAttribute('aria-checked', 'true');
});
