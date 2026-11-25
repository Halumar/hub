import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { BRAND } from '../src/lib/brand.ts';

/**
 * App shell / sidebar smoke test.
 * Verifies the authenticated layout renders: the wordmark, title, nav links and
 * the version pulled from the mocked /api/health endpoint.
 */
test.describe('App shell', () => {
	test('loads dashboard and renders sidebar with icon and version', async ({ page }) => {
		await setupApiMocks(page, { health: { status: 'ok', version: '9.9.9' } });

		await page.goto('/');

		// Sidebar brand: the wordmark alone (its h is the mark), named for screen readers.
		const sidebar = page.locator('aside');
		await expect(sidebar.getByRole('img', { name: BRAND.name })).toBeVisible();
		await expect(sidebar.getByRole('heading', { name: BRAND.name })).toBeVisible();
		await expect(sidebar.getByTestId('brand-wordmark')).toBeVisible();
		await expect(sidebar.getByTestId('brand-mark')).toHaveCount(0);

		// Navigation entries are present.
		for (const name of ['Overview', 'Areas', 'Devices', 'Guests', 'Notifications', 'Settings']) {
			await expect(sidebar.getByRole('link', { name, exact: true })).toBeVisible();
		}
		await expect(sidebar.getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page');

		// Version string comes from mocked /api/health.
		await expect(sidebar.getByText(`${BRAND.name} Hub 9.9.9`)).toBeVisible();

		// Main dashboard heading rendered.
		await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible();
	});

	// Brand rule: mark and wordmark never side by side or stacked.
	test('sign-in, Ingress-only page and phone bar show the wordmark without the mark', async ({ page }) => {
		await setupApiMocks(page, { session: { authenticated: false, direct_access: true } });
		await page.goto('/');
		await expect(page.getByLabel('Password')).toBeVisible();
		await expect(page.getByTestId('brand-wordmark')).toHaveCount(1);
		await expect(page.getByTestId('brand-mark')).toHaveCount(0);

		await setupApiMocks(page, { session: { authenticated: false, direct_access: false, ha_url: null } });
		await page.goto('/');
		await expect(page.getByTestId('ingress-only').getByTestId('brand-wordmark')).toBeVisible();
		await expect(page.getByTestId('brand-mark')).toHaveCount(0);

		await page.setViewportSize({ width: 390, height: 844 });
		await setupApiMocks(page, {});
		await page.goto('/');
		const bar = page.getByTestId('mobile-header');
		await expect(bar.getByTestId('brand-wordmark')).toBeVisible();
		await expect(bar.getByTestId('brand-mark')).toHaveCount(0);
	});

	test('settings › system names the license and the source code (EN, DE)', async ({ page }) => {
		const url = `${BRAND.urls.source}/hub`;
		for (const [lang, text] of [
			['en', `${BRAND.name} Hub is free software under the AGPL-3.0-only. Source code: ${url}`],
			['de', `${BRAND.name}-Hub ist freie Software unter der AGPL-3.0-only. Quellcode: ${url}`],
		]) {
			await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
			await setupApiMocks(page, {});
			await page.goto('/settings?tab=system');
			const line = page.getByTestId('source-offer');
			await expect(line).toHaveText(text);
			await expect(line.getByRole('link')).toHaveAttribute('href', url);
		}
	});
});
