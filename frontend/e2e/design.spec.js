import { test, expect } from '@playwright/test';
import { setupApiMocks, areaDetail, clientDetail } from './helpers.js';
import en from '../src/lib/i18n/en/index.js';
import de from '../src/lib/i18n/de/index.js';

/**
 * Admin page design: modal focus handling,
 * mobile area editor, i18n, theme and the card-style preset. Mocked API.
 */

const INGRESS = '/api/hassio_ingress/designtest';

function flatKeys(obj, prefix = '') {
	return Object.entries(obj).flatMap(([k, v]) => {
		const key = prefix ? `${prefix}.${k}` : k;
		return v && typeof v === 'object' && !('other' in v) ? flatKeys(v, key) : [key];
	});
}

test.describe('Modal focus management', () => {
	test('confirm dialog: focus inside, Tab stays inside, Esc closes and returns focus', async ({ page }) => {
		await setupApiMocks(page, { ingress: INGRESS });
		await page.goto('/areas');
		const trigger = page.getByRole('button', { name: 'Actions for Living Room' });
		await trigger.click();
		await page.getByTestId('area-delete').click();

		const dialog = page.getByRole('dialog', { name: 'Delete area?' });
		await expect(dialog).toBeVisible();
		// Initial focus on the safe action.
		await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused();

		// Tab / Shift+Tab never leave the dialog.
		for (let i = 0; i < 5; i++) {
			await page.keyboard.press('Tab');
			expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
		}
		for (let i = 0; i < 5; i++) {
			await page.keyboard.press('Shift+Tab');
			expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
		}

		await page.keyboard.press('Escape');
		await expect(dialog).toHaveCount(0);
		await expect(trigger).toBeFocused();
	});

	test('pairing dialog: Esc closes, cancels the PIN and returns focus to the trigger', async ({ page }) => {
		const requests = [];
		await setupApiMocks(page, {
			ingress: INGRESS,
			requests,
			handler: ({ path, method, json }) =>
				path === '/pairing/generate' && method === 'POST'
					? json({ pin: '1111-2222', expires_in: 300, expires_at: '', url: 'http://h:8099', deep_link: 'halumar://pair?pin=1111-2222' })
					: undefined,
		});
		await page.goto('/clients');
		const trigger = page.getByTestId('open-pairing');
		await trigger.click();
		const dialog = page.getByRole('dialog', { name: 'Pair device' });
		await expect(dialog.getByLabel('Owner')).toBeFocused();
		await dialog.getByRole('button', { name: 'Generate PIN' }).click();
		await expect(dialog.getByTestId('pairing-pin')).toHaveText('1111-2222');
		await page.keyboard.press('Escape');
		await expect(dialog).toHaveCount(0);
		await expect(trigger).toBeFocused();
		await expect.poll(() => requests.filter((r) => r === 'DELETE /pairing/cancel').length).toBe(1);
	});
});

test.describe('Area editor on a phone (390×844)', () => {
	test.use({ viewport: { width: 390, height: 844 } });

	test('fits the screen, has the bottom action bar, reorders sections and entities without drag', async ({ page }) => {
		const puts = [];
		await setupApiMocks(page, {
			ingress: INGRESS,
			handler: ({ path, method, request, json }) => {
				if (method === 'PUT' && (path.endsWith('/sections/order') || path.endsWith('/layout'))) {
					puts.push({ path, body: request.postDataJSON() });
					return json({ status: 'ok' });
				}
				return undefined;
			},
		});
		await page.goto('/areas/area-living-room');
		await expect(page.getByRole('heading', { name: 'Section 1' })).toBeVisible();

		// No horizontal overflow, one column.
		expect(await page.locator('main').evaluate((el) => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0);
		const bar = page.getByRole('group', { name: 'Add to area' });
		await expect(bar).toBeVisible();
		await expect(bar.getByRole('button', { name: 'Add entity' })).toBeVisible();
		const box = await bar.getByRole('button', { name: 'Add entity' }).boundingBox();
		expect(box.height).toBeGreaterThanOrEqual(44);

		// Section down (button, 44 px) -> PUT sections/order.
		await page.getByRole('button', { name: 'Move Section 1 down' }).click();
		await expect.poll(() => puts.find((p) => p.path.endsWith('/sections/order'))?.body).toEqual({
			section_ids: ['sec-1', 'sec-0', 'sec-2', 'sec-3'],
		});

		// Entity "…" menu -> Move down -> PUT layout with the new order.
		await page.getByRole('button', { name: 'Actions for Lamp 1.0' }).click();
		await page.getByTestId('item-move-down').click();
		await expect.poll(() => puts.filter((p) => p.path.endsWith('/layout')).length).toBe(1);
		const layout = puts.find((p) => p.path.endsWith('/layout')).body.items.filter((i) => i.section_id === 'sec-1');
		expect(layout.slice(0, 2).map((i) => i.entity_id)).toEqual(['light.s1_lamp_1', 'light.s1_lamp_0']);
	});
});

test.describe('i18n', () => {
	test('English and German dictionaries have the same keys', () => {
		const enKeys = flatKeys(en).sort();
		const deKeys = flatKeys(de).sort();
		expect(deKeys.filter((k) => !enKeys.includes(k))).toEqual([]);
		expect(enKeys.filter((k) => !deKeys.includes(k))).toEqual([]);
	});

	test.describe('German browser', () => {
		test.use({ locale: 'de-CH' });
		test('follows the browser language', async ({ page }) => {
			await setupApiMocks(page);
			await page.goto('/areas');
			await expect(page.getByRole('heading', { name: 'Bereiche', level: 1 })).toBeVisible();
			await expect(page.locator('html')).toHaveAttribute('lang', 'de');
		});
	});

	test('switching the language in the settings applies at once and is kept', async ({ page }) => {
		await setupApiMocks(page, { ingress: INGRESS });
		await page.goto('/settings');
		await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();
		await page.getByTestId('language-switch').getByRole('button', { name: 'Deutsch' }).click();
		await expect(page.getByRole('heading', { name: 'Einstellungen', level: 1 })).toBeVisible();
		await expect(page.locator('aside').getByRole('link', { name: 'Geräte' })).toBeVisible();
		await page.reload();
		await expect(page.getByRole('heading', { name: 'Einstellungen', level: 1 })).toBeVisible();
		await page.getByTestId('language-switch').getByRole('button', { name: 'Automatisch' }).click();
		await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();
	});
});

test.describe('Theme', () => {
	test('follows prefers-color-scheme and can be overridden', async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'dark' });
		await setupApiMocks(page, { ingress: INGRESS });
		await page.goto('/settings');
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
		await page.getByTestId('theme-switch').getByRole('button', { name: 'Light' }).click();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await page.getByTestId('theme-switch').getByRole('button', { name: 'Automatic' }).click();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
	});
});

test.describe('Accessibility basics', () => {
	for (const path of ['/', '/areas', '/areas/area-living-room', '/clients', '/clients/client-1234abcd', '/guests', '/notifications', '/settings']) {
		test(`every button and link on ${path} has an accessible name`, async ({ page }) => {
			await setupApiMocks(page, { ingress: INGRESS });
			await page.goto(path);
			await page.waitForLoadState('networkidle');
			await page.waitForTimeout(300);
			const unnamed = await page.evaluate(() =>
				[...document.querySelectorAll('button, a[href], [role="switch"], input:not([type="hidden"]), select, textarea')]
					.filter((el) => el.offsetParent !== null)
					.filter((el) => {
						const label =
							el.getAttribute('aria-label') ||
							(el.getAttribute('aria-labelledby') || '')
								.split(' ')
								.map((id) => document.getElementById(id)?.textContent || '')
								.join('') ||
							(el.id && document.querySelector(`label[for="${el.id}"]`)?.textContent) ||
							el.closest('label')?.textContent ||
							el.textContent ||
							el.getAttribute('title') ||
							el.getAttribute('placeholder');
						return !label || !label.trim();
					})
					.map((el) => el.outerHTML.slice(0, 120)),
			);
			expect(unnamed).toEqual([]);
		});
	}
});
