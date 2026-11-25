import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * Areas critical-path tests against a mocked /api/areas list.
 */
test.describe('Areas', () => {
	test('navigates to Areas and shows the mocked list', async ({ page }) => {
		await setupApiMocks(page);

		await page.goto('/');
		await page.locator('aside').getByRole('link', { name: 'Areas' }).click();

		await expect(page).toHaveURL(/\/areas/);
		await expect(page.getByRole('heading', { name: 'Areas', level: 1 })).toBeVisible();

		// Both mocked areas render.
		await expect(page.getByRole('heading', { name: 'Living Room' })).toBeVisible();
		await expect(page.getByRole('heading', { name: 'Kitchen' })).toBeVisible();
	});

	test('shows empty state when no areas exist', async ({ page }) => {
		await setupApiMocks(page, { areas: [] });

		await page.goto('/areas');

		await expect(page.getByText('No areas configured')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Create Area' })).toBeVisible();
	});

	test('opens the new-area form and validates empty submit', async ({ page }) => {
		await setupApiMocks(page);

		await page.goto('/areas');

		// Open the create modal.
		await page.getByRole('button', { name: /New Area/ }).click();

		const dialog = page.getByRole('dialog');
		await expect(dialog.getByRole('heading', { name: 'New Area' })).toBeVisible();

		// Submit with an empty name -> inline validation error appears.
		await dialog.getByRole('button', { name: 'Create Area' }).click();
		await expect(dialog.getByText('Name is required')).toBeVisible();

		// Modal stays open (creation was blocked by validation).
		await expect(dialog.getByRole('heading', { name: 'New Area' })).toBeVisible();
	});

	test('a sync from Home Assistant names what it left out', async ({ page }) => {
		// Every entry of result.errors is shown.
		const errors = [
			"Failed to import area 'Garage': at most 100 areas",
			"Area 'Keller': 12 entities not added (at most 20 sections of 50)",
			'third',
			'fourth',
		];
		await setupApiMocks(page, {
			handler: ({ path, method, json }) => {
				if (path === '/ha/areas' && method === 'GET') return json([{ area_id: 'garage', name: 'Garage' }]);
				if (path === '/sync/from-ha' && method === 'POST')
					return json({ synced: 1, created: 0, updated: 0, entities_added: 0, errors });
			},
		});
		await page.goto('/areas');
		await page.getByRole('button', { name: /Import from Home Assistant/ }).first().click();
		await page.getByRole('button', { name: /Import all/ }).click();
		await expect(page.getByText(/4 items not taken over: .*at most 100 areas.*12 entities not added.*third \(\+1\)/)).toBeVisible();
	});
});
