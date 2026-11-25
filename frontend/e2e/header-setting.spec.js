import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';

/**
 * "Header with clock & weather" as a managed
 * setting on the device page and in templates; area pins from the area
 * choice when pairing by iPhone shown by name; iPad pairings by iPhone.
 */

const CLIENT = 'client-1234abcd';

async function openDevice(page, overrides = {}) {
	const puts = [];
	let current = clientDetail({ id: CLIENT, name: 'iPad Hallway', ...overrides });
	await setupApiMocks(page, {
		handler: ({ path, method, request, json }) => {
			if (path === `/pairing/clients/${CLIENT}/app-overrides` && method === 'PUT') {
				const body = request.postDataJSON().overrides;
				puts.push(body);
				const next = { ...current.app_config_overrides };
				for (const [k, v] of Object.entries(body)) v === null ? delete next[k] : (next[k] = v);
				current = { ...current, app_config_overrides: next };
				return json({ app_config_overrides: next });
			}
			if (path === `/pairing/clients/${CLIENT}` && method === 'PATCH') {
				const body = request.postDataJSON();
				puts.push(body);
				current = { ...current, ...body };
				return json(current);
			}
			if (path === `/pairing/clients/${CLIENT}` && method === 'GET') return json(current);
			return undefined;
		},
	});
	await page.goto(`/clients/${CLIENT}`);
	return puts;
}

test('running iPad pairing shows the iPad', async ({ page }) => {
	const session = {
		id: '7c2f0e2a-3c4d-4e5f-8a9b-0c1d2e3f4a5b',
		client_name: 'iPad Hallway',
		platform: 'ipados',
		app_version: '2026.12.0',
		device_model: 'iPad16,3',
		created_at: new Date().toISOString(),
		expires_at: new Date(Date.now() + 240_000).toISOString(),
		source_ip: '192.0.2.51',
	};
	await setupApiMocks(page, {
		handler: ({ path, method, json }) => {
			if (path === '/pairing/onboarding' && method === 'GET') return json({ sessions: [session], expires_in: 300 });
			return undefined;
		},
	});
	await page.goto('/clients');
	const card = page.getByTestId('onboarding-sessions');
	await expect(card.getByRole('heading', { name: 'Set-ups in progress' })).toBeVisible();
	await expect(card.getByText(/iPadOS · iPad16,3 · 2026\.12\.0/)).toBeVisible();
	await expect(card.getByText('The PIN always works.', { exact: false })).toBeVisible();
});
