import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * Notifications navigation smoke test (mocked, empty list).
 */
test.describe('Notifications', () => {
	test('navigates to Notifications and renders the page', async ({ page }) => {
		await setupApiMocks(page);

		await page.goto('/');
		await page.locator('aside').getByRole('link', { name: 'Notifications' }).click();

		await expect(page).toHaveURL(/\/notifications/);
		await expect(page.getByRole('heading', { name: 'Notifications', level: 1 })).toBeVisible();

		// Compose action and empty state are present.
		await expect(page.getByRole('button', { name: /Compose/ }).first()).toBeVisible();
		await expect(page.getByText('No notifications')).toBeVisible();
	});
});
