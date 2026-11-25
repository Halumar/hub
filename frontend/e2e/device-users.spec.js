import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';
import { BRAND } from '../src/lib/brand.ts';

/**
 * One Home Assistant user per device: owner label and remote access in
 * the client page and the pairing dialog, setup state while the integration
 * creates the user. Against the mocked API (see helpers.js).
 */

const CLIENT = 'client-1234abcd';

test('pending device shows the setup state and a hint after two minutes', async ({ page }) => {
	await page.clock.setFixedTime(new Date('2026-11-18T10:10:00Z'));
	await setupApiMocks(page, {
		handler: ({ path, method, json }) =>
			path === `/pairing/clients/${CLIENT}` && method === 'GET'
				? json(
						clientDetail({
							id: CLIENT,
							has_ha_token: false,
							ha_user_id: null,
							ha_token_status: 'pending',
							ha_token_pending_since: '2026-11-18T10:00:00Z',
						}),
					)
				: undefined,
	});
	await page.goto(`/clients/${CLIENT}`);
	const card = page.getByTestId('ha-user-card');
	await expect(card.getByTestId('ha-user-pending')).toHaveText('Setting up…');
	await expect(card.getByTestId('ha-user-slow')).toBeVisible();
	await expect(card.getByRole('button', { name: 'Renew HA token' })).toHaveCount(0);
});

