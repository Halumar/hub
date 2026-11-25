import { test, expect } from '@playwright/test';
import { setupApiMocks, areaDetail, clientDetail } from './helpers.js';
import { BRAND } from '../src/lib/brand.ts';

/**
 * Pairing dialog and admin WebSocket of the admin UI.
 * All against the mocked API (see helpers.js).
 */

const INGRESS = '/api/hassio_ingress/e2etoken';

test.describe('Admin WebSocket', () => {
	test('answers server pings with pong', async ({ page }) => {
		const fromClient = [];
		await setupApiMocks(page, {
			ws: (ws) => {
				ws.onMessage((m) => fromClient.push(JSON.parse(String(m))));
				ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
				ws.send(JSON.stringify({ type: 'ping' }));
			},
		});
		await page.goto('/');
		await expect.poll(() => fromClient.filter((m) => m.type === 'pong').length).toBe(1);
		await expect(page.locator('aside [data-ws-status]')).toHaveAttribute('data-ws-status', 'connected');
		await expect(page.locator('aside').getByText('Live', { exact: true })).toBeVisible();
	});

	test('badge is not "Live" when the server does not authenticate the session', async ({ page }) => {
		let closed = false;
		await setupApiMocks(page, {
			ws: (ws) => {
				ws.onClose(() => { closed = true; });
				ws.send(JSON.stringify({ type: 'auth_required', message: 'Send auth', timeout: 30 }));
			},
		});
		await page.goto('/');
		const badge = page.locator('aside [data-ws-status]');
		await expect(badge).toHaveAttribute('data-ws-status', 'limited');
		await expect(badge).toContainText('No live updates');
		await expect(page.locator('aside').getByText('Live', { exact: true })).toHaveCount(0);
		// The client gives up the unauthenticated socket instead of waiting for the 30 s timeout.
		await expect.poll(() => closed).toBe(true);
	});

	test('does not open the WebSocket before login', async ({ page }) => {
		let connections = 0;
		await setupApiMocks(page, {
			session: { authenticated: false },
			ws: (ws) => {
				connections++;
				ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
			},
		});
		await page.goto('/');
		await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
		await page.waitForTimeout(500);
		expect(connections).toBe(0);

		await page.getByLabel('Username').fill('admin');
		await page.getByLabel('Password').fill('secret');
		await page.getByRole('button', { name: 'Sign in' }).click();
		await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible();
		await expect.poll(() => connections).toBe(1);
	});
});

test.describe('Session expiry', () => {
	test('a 401 returns to the login form instead of showing defaults', async ({ page }) => {
		await setupApiMocks(page, {
			handler: ({ path, json }) => {
				if (path === '/auth/session' || path === '/health') return undefined;
				return json({ detail: 'Unauthorized' }, 401);
			},
		});
		await page.goto('/settings');
		await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
		await expect(page.getByText('Your session has expired')).toBeVisible();
		await expect(page.getByText('No clients paired yet')).toHaveCount(0);
	});

	test('a WebSocket close with 4401 also returns to the login form', async ({ page }) => {
		await setupApiMocks(page, {
			ws: (ws) => {
				ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'admin' }));
				setTimeout(() => ws.close({ code: 4401, reason: 'session expired' }), 300);
			},
		});
		await page.goto('/');
		await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible();
	});
});

test.describe('Client detail', () => {

});

/** Area list: "Properties" lives in the row's "…" menu. */
async function openProperties(page, name) {
	await page.getByRole('button', { name: `Actions for ${name}` }).click();
	await page.getByRole('menuitem', { name: 'Properties' }).click();
}

test.describe('Area properties modal', () => {
	test('saves only changed fields: control_mode null and missing color stay untouched', async ({ page }) => {
		const patches = [];
		await setupApiMocks(page, {
			ingress: INGRESS,
			handler: ({ path, method, request, json }) => {
				if (method === 'PATCH' && path.startsWith('/areas/')) {
					patches.push(request.postDataJSON());
					return json({ success: true });
				}
				return undefined;
			},
		});
		await page.goto('/areas');
		// Kitchen: color null, control_mode 'basic'; Living Room: control_mode null.
		await openProperties(page, 'Living Room');
		const dialog = page.getByRole('dialog', { name: 'Area properties' });
		await expect(dialog.getByLabel('Control mode')).toHaveValue('');
		await dialog.getByLabel('Description').fill('Updated lounge');
		await dialog.getByRole('button', { name: 'Save Changes' }).click();
		await expect(dialog).toHaveCount(0);
		expect(patches).toEqual([{ description: 'Updated lounge' }]);
	});

	test('area list and area editor use the same modal', async ({ page }) => {
		await setupApiMocks(page, { ingress: INGRESS });
		await page.goto('/areas');
		await openProperties(page, 'Kitchen');
		const fromList = page.getByRole('dialog', { name: 'Area properties' });
		await expect(fromList.getByLabel('Control mode').locator('option')).toHaveText([/^Default/, 'Basic', 'Advanced', 'Full']);
		await fromList.getByRole('button', { name: 'Cancel' }).click();

		await page.goto('/areas/area-kitchen');
		await page.getByRole('button', { name: 'Properties', exact: true }).click();
		const fromEditor = page.getByRole('dialog', { name: 'Area properties' });
		await expect(fromEditor.getByLabel('Control mode')).toHaveValue('basic');
		await expect(fromEditor.getByLabel('Control mode').locator('option')).toHaveText([/^Default/, 'Basic', 'Advanced', 'Full']);
	});

	test('removing an image does not submit the form', async ({ page }) => {
		const requests = [];
		await setupApiMocks(page, {
			ingress: INGRESS,
			requests,
			handler: ({ path, method, json }) => {
				if (path === '/areas/area-living-room' && method === 'GET')
					return json(areaDetail('area-living-room', { card_image_path: 'card.png', card_image_src: '/images/card.png' }));
				if (method === 'DELETE') return json(null, 200);
				return undefined;
			},
		});
		await page.route('**/images/card.png', (r) => r.fulfill({ status: 200, contentType: 'image/png', body: Buffer.alloc(0) }));
		await page.goto('/areas/area-living-room');
		await page.getByRole('button', { name: 'Properties', exact: true }).click();
		const dialog = page.getByRole('dialog', { name: 'Area properties' });
		await dialog.getByRole('textbox', { name: 'Name', exact: true }).fill('Unsaved new name');
		await dialog.getByRole('button', { name: 'Remove Card Image' }).click();
		await expect.poll(() => requests.filter((r) => r.startsWith('DELETE')).length).toBe(1);
		await page.waitForTimeout(300);
		expect(requests.filter((r) => r.startsWith('PATCH'))).toEqual([]);
		await expect(dialog).toBeVisible();
		await expect(dialog.getByRole('textbox', { name: 'Name', exact: true })).toHaveValue('Unsaved new name');
	});

	test('save errors are shown inside the modal', async ({ page }) => {
		await setupApiMocks(page, {
			ingress: INGRESS,
			handler: ({ method, path, json }) =>
				method === 'PATCH' && path.startsWith('/areas/') ? json({ detail: 'Name already exists' }, 409) : undefined,
		});
		await page.goto('/areas');
		await openProperties(page, 'Living Room');
		const dialog = page.getByRole('dialog', { name: 'Area properties' });
		await dialog.getByRole('textbox', { name: 'Name', exact: true }).fill('Kitchen');
		await dialog.getByRole('button', { name: 'Save Changes' }).click();
		await expect(dialog.getByRole('alert')).toContainText('Name already exists');
	});
});

test.describe('Create area modal', () => {
	test('server errors appear inside the modal, not behind it', async ({ page }) => {
		await setupApiMocks(page, {
			handler: ({ method, path, json }) =>
				method === 'POST' && path === '/areas' ? json({ detail: 'Too many areas' }, 400) : undefined,
		});
		await page.goto('/areas');
		await page.getByRole('button', { name: /New Area/ }).click();
		const dialog = page.getByRole('dialog');
		await dialog.getByPlaceholder('e.g. Living Room, Garage').fill('Attic');
		await dialog.getByRole('button', { name: 'Create Area' }).click();
		await expect(dialog.getByRole('alert')).toContainText('Too many areas');
	});
});

test.describe('API client', () => {
	test('write requests are not retried on network errors', async ({ page }) => {
		let posts = 0;
		await setupApiMocks(page);
		await page.route('**/api/areas', (route) => {
			if (route.request().method() === 'POST') {
				posts++;
				return route.abort('connectionreset');
			}
			return route.fallback();
		});
		await page.goto('/areas');
		await page.getByRole('button', { name: /New Area/ }).click();
		const dialog = page.getByRole('dialog');
		await dialog.getByPlaceholder('e.g. Living Room, Garage').fill('Attic');
		await dialog.getByRole('button', { name: 'Create Area' }).click();
		await expect(dialog.getByRole('alert')).toContainText('Network error');
		await page.waitForTimeout(1600); // longer than the full retry backoff
		expect(posts).toBe(1);
	});

	test('idempotent GETs are still retried and end in a visible error', async ({ page }) => {
		let gets = 0;
		await setupApiMocks(page);
		await page.route('**/api/areas', (route) => {
			if (route.request().method() === 'GET') {
				gets++;
				return route.abort('connectionreset');
			}
			return route.fallback();
		});
		await page.goto('/areas');
		await expect(page.getByText(/Could not load areas/)).toBeVisible({ timeout: 5000 });
		expect(gets).toBe(4); // initial attempt + 3 retries
		await expect(page.getByText('No areas configured')).toHaveCount(0);
	});
});

test.describe('Settings', () => {
	test('failed loaders show an error state and disable the toggles', async ({ page }) => {
		await setupApiMocks(page, {
			ingress: INGRESS,
			handler: ({ path, json }) =>
				path === '/settings/compliance-endpoint' || path === '/settings/pairing-via-integration' || path === '/pairing/clients'
					? json({ detail: 'boom' }, 400)
					: undefined,
		});
		await page.goto('/settings');
		await expect(page.getByTestId('settings-load-error')).toContainText('Paired devices');
		await expect(page.getByText('No clients paired yet')).toHaveCount(0);
		await page.getByRole('tab', { name: 'Advanced' }).click();
		await expect(page.getByRole('switch', { name: 'Enable device compliance status' })).toBeDisabled();
		await expect(page.getByRole('switch', { name: 'Allow pairing via the integration' })).toBeDisabled();
	});

	test('the active tab is kept in the URL', async ({ page }) => {
		await setupApiMocks(page, { ingress: INGRESS });
		await page.goto('/settings');
		await page.getByRole('tab', { name: 'Advanced' }).click();
		await expect(page).toHaveURL(/[?&]tab=advanced/);
		await page.reload();
		await expect(page.getByRole('tab', { name: 'Advanced' })).toHaveAttribute('aria-selected', 'true');
	});

	// Now 2026-11-18 00:24 UTC; the default is 24 h later, shown in the browser's zone.
	for (const [zone, expected] of [
		['Europe/Zurich', '2026-11-19T01:24'],
		['UTC', '2026-11-19T00:24'],
		['America/Los_Angeles', '2026-11-18T16:24'],
	]) {
		test.describe(`guest expiry in ${zone}`, () => {
			test.use({ timezoneId: zone });
			test(`guest expiry date defaults to local time (+24 h, ${zone})`, async ({ page }) => {
				await page.clock.setFixedTime(new Date('2026-11-18T00:24:00Z'));
				await setupApiMocks(page, { ingress: INGRESS });
				await page.goto('/guests');
				await page.getByTestId('open-pairing-guest').click();
				const dialog = page.getByRole('dialog', { name: 'Invite guest' });
				await dialog.getByTestId('pairing-expiry-date').click();
				await expect(dialog.locator('input[type="datetime-local"]')).toHaveValue(expected);
				// The expiry is also spelled out in local time (INDEX webui/107).
				await expect(dialog.getByTestId('pairing-expiry-text')).toContainText(String(Number(expected.slice(8, 10))));
			});
		});
	}

	test('pairing under Ingress shows the TLS URL, not http://…:8099', async ({ page }) => {
		await setupApiMocks(page, {
			ingress: INGRESS,
			handler: ({ path, json }) =>
				path === '/pairing/status'
					? json({
						pairing_active: true,
						pin: '1234-5678',
						remaining_seconds: 290,
						url: 'http://198.51.100.178:8099',
						deep_link: 'halumar://pair?url=http%3A%2F%2F198.51.100.178%3A8099&pin=1234-5678&tls_port=8100&fp=5a8e8ee2aaaabbbbccccddddeeeeffff00001111222233334444555660d073',
					})
					: undefined,
		});
		await page.goto('/clients');
		await page.getByTestId('open-pairing').click();
		const dialog = page.getByRole('dialog', { name: 'Pair device' });
		await expect(dialog.getByTestId('pairing-url')).toHaveText('https://198.51.100.178:8100');
		await expect(dialog.getByTestId('pairing-fingerprint')).toHaveText('5a8e8ee2…0d073');
		await expect(dialog.getByTestId('pairing-pin')).toHaveText('1234-5678');
	});
});

test.describe('Guest expiry on the client page', () => {
	// The guest expires 2026-11-18 12:00 UTC; extending by date proposes 24 h later in local time.
	for (const [zone, expected] of [
		['Europe/Zurich', '2026-11-19T13:00'],
		['UTC', '2026-11-19T12:00'],
		['America/Los_Angeles', '2026-11-19T04:00'],
	]) {
		test.describe(zone, () => {
			test.use({ timezoneId: zone });
			test(`extend-by-date default is local time (${zone})`, async ({ page }) => {
				await page.clock.setFixedTime(new Date('2026-11-18T00:24:00Z'));
				await setupApiMocks(page, {
					ingress: INGRESS,
					handler: ({ path, json }) =>
						path === '/pairing/clients/client-1234abcd'
							? json(clientDetail({ access: 'guest', expires_at: '2026-11-18T12:00:00Z' }))
							: undefined,
				});
				await page.goto('/clients/client-1234abcd');
				await page.getByTestId('fact-extend').click();
				await page.getByRole('button', { name: /Date/ }).first().click();
				await expect(page.locator('input[type="datetime-local"]')).toHaveValue(expected);
			});
		});
	}
});

test.describe('Area editor without full-page spinner', () => {
	test('live events and own changes reload quietly and keep the scroll position', async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 600 });
		let sendToClient;
		let getCount = 0;
		await setupApiMocks(page, {
			ingress: INGRESS,
			ws: (ws) => {
				sendToClient = (m) => ws.send(JSON.stringify(m));
				ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
			},
			handler: async ({ path, method }) => {
				if (path === '/areas/area-living-room' && method === 'GET') {
					getCount++;
					await new Promise((r) => setTimeout(r, 300));
				}
				return undefined;
			},
		});
		await page.goto('/areas/area-living-room');
		await expect(page.getByText('Lamp 3.3')).toBeVisible();
		const main = page.locator('main');
		await main.evaluate((el) => el.scrollTo(0, 300));
		const before = await main.evaluate((el) => el.scrollTop);
		expect(before).toBeGreaterThan(100);

		const loadsBefore = getCount;
		// A burst of live events -> one quiet reload.
		for (let i = 0; i < 5; i++) sendToClient({ type: 'entity_updated', data: { area_id: 'area-living-room' } });
		await page.waitForTimeout(100);
		await expect(page.locator('main .animate-spin')).toHaveCount(0);
		await page.waitForTimeout(800);
		expect(getCount - loadsBefore).toBe(1);

		// Own change (width of a tile) -> no spinner either.
		await page.getByRole('group', { name: 'Width of Lamp 1.2' }).getByRole('button', { name: '2' }).click();
		await page.waitForTimeout(100);
		await expect(page.locator('main .animate-spin')).toHaveCount(0);
		await page.waitForTimeout(500);
		expect(await main.evaluate((el) => el.scrollTop)).toBe(before);
	});
});

test.describe('Mobile under Ingress', () => {
	test.use({ viewport: { width: 390, height: 844 } });

	test('slim header without the duplicated app name', async ({ page }) => {
		await setupApiMocks(page, { ingress: INGRESS });
		await page.goto('/areas/area-living-room');
		const header = page.getByTestId('mobile-header');
		await expect(header).toBeVisible();
		await expect(header.getByRole('heading', { name: `${BRAND.name}` })).toHaveCount(0);
		const box = await header.boundingBox();
		expect(box.height).toBeLessThan(50);
	});

	test('area editor sections fit the screen (one column)', async ({ page }) => {
		// Half-width sections (columns 1) sat side by side on phones before.
		const half = areaDetail('area-living-room');
		half.sections = half.sections.map((sec) => ({ ...sec, columns: 1 }));
		await setupApiMocks(page, {
			ingress: INGRESS,
			handler: ({ path, method, json }) =>
				path === '/areas/area-living-room' && method === 'GET' ? json(half) : undefined,
		});
		await page.goto('/areas/area-living-room');
		await expect(page.getByText('Lamp 0.0')).toBeVisible();
		const overflow = await page.locator('main').evaluate((el) => el.scrollWidth - el.clientWidth);
		expect(overflow).toBeLessThanOrEqual(0);
		const first = await page.getByRole('heading', { name: 'Section 1' }).boundingBox();
		const second = await page.getByRole('heading', { name: 'Section 2' }).boundingBox();
		expect(second.y).toBeGreaterThan(first.y + 20); // stacked, not side by side
	});
});

test.describe('Bundle', () => {
	test('MDI icon data is only fetched when an icon picker needs it', async ({ page }) => {
		const mdiRequests = [];
		page.on('request', (r) => { if (/mdi-icons/.test(r.url())) mdiRequests.push(r.url()); });
		await setupApiMocks(page, {
			areas: [{ id: 'a1', name: 'Plain', icon: 'sf:house', grid_cols: 2, is_active: true, tags: [], entity_count: 0 }],
		});
		await page.goto('/areas');
		await expect(page.getByRole('heading', { name: 'Plain' })).toBeVisible();
		await page.waitForTimeout(300);
		expect(mdiRequests).toEqual([]);

		await page.getByRole('button', { name: /New Area/ }).click();
		await page.getByRole('dialog').getByPlaceholder(/Search MDI/).click();
		await expect.poll(() => mdiRequests.length).toBeGreaterThan(0);
		await page.getByRole('dialog').getByPlaceholder(/Search MDI/).fill('sofa');
		await expect(page.getByRole('dialog').getByTitle('mdi:sofa', { exact: true })).toBeVisible();
	});
});
