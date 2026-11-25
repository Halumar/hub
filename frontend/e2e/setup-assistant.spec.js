import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * Setup assistant: opens once on a fresh hub, recognizes steps done
 * elsewhere, stores progress (skip / later / finish), imports a first area
 * from HA, pairs a device without leaving the assistant; equal
 * buttons and tiles (DE/EN, desktop and phone) and focus handling.
 */

const MY_LINK = 'https://my.home-assistant.io/redirect/config_flow_start/?domain=halumar';
const REPO_LINK = 'https://my.home-assistant.io/redirect/hacs_repository/?owner=Halumar&repository=integration&category=integration'; // brandlint: allow
const HA_AREAS = [
	{ area_id: 'wohnzimmer', name: 'Wohnzimmer' },
	{ area_id: 'kueche', name: 'Küche' },
	{ area_id: 'schlafzimmer', name: 'Schlafzimmer' },
	{ area_id: 'buero', name: 'Büro' },
	{ area_id: 'eingang', name: 'Eingang' },
	{ area_id: 'garage', name: 'Garage' },
	{ area_id: 'garten', name: 'Garten' },
];
const CONNECTED = {
	connected: true,
	push_connected: true,
	last_request_ts: Date.now() / 1000 - 12,
	request_count: 42,
	auth_failure_count: 0,
	integration_version: '2026.12.0',
	addon_version: '2026.12.0',
	ha_version: '2026.11.2',
	ha_domain: 'halumar',
};
const NEVER = { connected: false, push_connected: false, last_request_ts: null, request_count: 0, auth_failure_count: 0, integration_version: null, ha_version: '2026.11.2', ha_domain: 'halumar' };
const AREA = { id: 'a-wohnzimmer', name: 'Wohnzimmer', icon: 'mdi:sofa', is_active: true, sort_order: 0, tags: [], entity_count: 6, ha_area_id: 'wohnzimmer' };
const DEVICE = {
	id: 'c-iphone',
	name: 'Owner B iPhone',
	owner_label: 'Owner B',
	is_active: true,
	is_connected: true,
	is_guest: false,
	tags: [],
	ha_token_status: 'active',
	created_at: '2026-11-19T08:00:00Z',
	last_seen: '2026-11-19T08:00:00Z',
	platform: 'ios',
};

/**
 * Mocked hub with mutable state. Returns the state object (setup, puts,
 * syncs, created) so tests can assert on what the UI sent.
 */
async function hub(page, opts = {}) {
	const s = {
		setup: opts.setup || { status: 'new', step: 'integration', skipped: [] },
		integration: opts.integration || NEVER,
		token: opts.token || { has_token: true, supervisor_managed: true },
		haUrls: opts.haUrls || { ha_url: '', ha_internal_url: '', ha_url_detected: '', ha_internal_url_detected: 'http://198.51.100.178' },
		areas: opts.areas || [],
		clients: opts.clients || [],
		puts: [],
		syncs: [],
		created: [],
		socket: null,
	};
	if (opts.lang) {
		await page.addInitScript((l) => localStorage.setItem('hub.lang', l), opts.lang);
	}
	await setupApiMocks(page, {
		areas: s.areas,
		clients: s.clients,
		ws: (ws) => {
			s.socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
		handler: ({ path, method, request, json }) => {
			if (path === '/admin/setup-assistant') {
				if (method === 'PUT') {
					const body = request.postDataJSON();
					s.puts.push(body);
					s.setup = body;
					return json(body);
				}
				return json(s.setup);
			}
			if (path === '/admin/integration-status') return json(s.integration);
			if (path === '/auth/integration-token') return json(s.token);
			if (path === '/settings/ha-urls') {
				if (method === 'PUT') {
					const body = request.postDataJSON();
					s.haUrls = { ...s.haUrls, ha_url: body.ha_url || '', ha_internal_url: body.ha_internal_url || '' };
				}
				return json(s.haUrls);
			}
			if (path === '/ha/areas') return json(HA_AREAS);
			if (path === '/areas' && method === 'GET') return json(s.areas);
			if (path === '/areas' && method === 'POST') {
				const body = request.postDataJSON();
				s.created.push(body);
				const area = { id: `a-${s.created.length}`, entity_count: 0, tags: [], ...body };
				s.areas = [...s.areas, area];
				return json(area, 201);
			}
			if (path === '/sync/from-ha' && method === 'POST') {
				const body = request.postDataJSON();
				s.syncs.push(body);
				const add = HA_AREAS.filter((a) => body.area_ids.includes(a.area_id)).map((a) => ({ ...AREA, id: `a-${a.area_id}`, name: a.name, ha_area_id: a.area_id }));
				s.areas = [...s.areas, ...add];
				return json({ synced: add.length, created: add.length, updated: 0, entities_added: 5, errors: opts.syncErrors || [] });
			}
			if (path === '/pairing/clients') return json(s.clients);
			if (path === '/settings/guest-defaults') return json({ all_areas: false, tags: [], area_ids: [], template_id: null, expires_hours: 24 });
			return undefined;
		},
	});
	return s;
}

const panel = (page, step) => page.getByTestId(`setup-panel-${step}`);

test.describe('setup assistant', () => {
	test('a fresh hub opens the assistant once, at step 1 with the My Home Assistant link', async ({ page }) => {
		const s = await hub(page, { lang: 'en' });
		await page.goto('/');
		await expect(page).toHaveURL(/\/setup$/);
		await expect(panel(page, 'integration')).toBeVisible();
		await expect(page.getByTestId('setup-my-link')).toHaveAttribute('href', MY_LINK);
		await expect(page.getByTestId('setup-my-link')).toHaveAttribute('target', '_blank');
		await expect(page.getByTestId('setup-repo-link')).toHaveAttribute('href', REPO_LINK);
		await expect(panel(page, 'integration').getByText('Marketplace', { exact: false })).toBeVisible();
		await expect(panel(page, 'integration').getByText('HACS', { exact: false })).toBeVisible();
		await expect.poll(() => s.puts.at(-1)).toEqual({ status: 'in_progress', step: 'integration', skipped: [] });

		// Continue later: stored as dismissed, the overview no longer redirects.
		await page.getByTestId('setup-later').click();
		await expect(page).toHaveURL(/\/$/);
		expect(s.puts.at(-1).status).toBe('dismissed');
		await expect(page.getByTestId('setup-checklist')).toBeVisible();
		await page.reload();
		await expect(page.getByTestId('setup-checklist')).toBeVisible();
		await expect(page).toHaveURL(/\/$/);
	});

	test('the integration step ticks itself off once the integration reports in', async ({ page }) => {
		const s = await hub(page, { lang: 'en' });
		await page.goto('/setup');
		await expect(page.getByTestId('setup-step-integration')).toHaveAttribute('data-state', 'open');
		s.integration = CONNECTED;
		await page.getByTestId('setup-recheck').click();
		await expect(page.getByTestId('setup-integration-ok')).toContainText('2026.12.0');
		await expect(page.getByTestId('setup-step-integration')).toHaveAttribute('data-state', 'done');
		await expect(page.getByTestId('setup-skip')).toBeDisabled();
		await page.getByTestId('setup-next-btn').click();
		await expect(page.getByTestId('setup-connection-ok')).toBeVisible();
		await expect(page.getByTestId('setup-checks')).toContainText('Automatic');
		await expect(page.getByTestId('setup-checks')).toContainText('live push');
	});

	test('steps done elsewhere are recognized; the assistant starts at the first open one', async ({ page }) => {
		await hub(page, { lang: 'en', setup: { status: 'dismissed', step: 'integration', skipped: [] }, integration: CONNECTED, areas: [AREA] });
		await page.goto('/setup');
		await expect(panel(page, 'device')).toBeVisible();
		for (const step of ['integration', 'connection', 'urls', 'area']) {
			await expect(page.getByTestId(`setup-step-${step}`)).toHaveAttribute('data-state', 'done');
		}
		await expect(page.getByTestId('setup-step-device')).toHaveAttribute('aria-current', 'step');
		await expect(page.getByTestId('setup-step-device')).toHaveAttribute('data-state', 'open');
	});

	test('saved progress is resumed', async ({ page }) => {
		await hub(page, { lang: 'en', setup: { status: 'in_progress', step: 'urls', skipped: ['integration'] } });
		await page.goto('/setup');
		await expect(panel(page, 'urls')).toBeVisible();
		await expect(page.getByTestId('setup-step-integration')).toHaveAttribute('data-state', 'skipped');
		await expect(page.getByLabel('Internal address (home network)')).toHaveAttribute('placeholder', 'http://198.51.100.178');
	});

	test('skip, step list, summary and finish are stored', async ({ page }) => {
		const s = await hub(page, { lang: 'de' });
		await page.goto('/setup');
		await page.getByTestId('setup-skip').click();
		await expect(panel(page, 'connection')).toBeVisible();
		await expect(page.locator('#setup-step-title')).toBeFocused();
		await expect(page.getByTestId('setup-step-integration')).toHaveAttribute('data-state', 'skipped');
		await expect.poll(() => s.puts.at(-1)).toEqual({ status: 'in_progress', step: 'connection', skipped: ['integration'] });

		await page.getByTestId('setup-back').click();
		await expect(panel(page, 'integration')).toBeVisible();
		await page.getByTestId('setup-step-done').click();
		await expect(panel(page, 'done')).toBeVisible();
		const summary = page.getByTestId('setup-summary');
		await expect(summary.locator('li[data-state="skipped"]')).toHaveCount(1);
		await expect(summary.getByText('übersprungen')).toBeVisible();
		await expect(page.getByRole('heading', { name: 'Fast geschafft' })).toBeVisible();
		await expect(page.getByTestId('setup-next').getByRole('link')).toHaveCount(4);
		await expect(page.getByTestId('setup-next')).toContainText('Pro');

		await summary.getByRole('button', { name: 'Zu „Erster Bereich“' }).click();
		await expect(panel(page, 'area')).toBeVisible();
		await page.getByTestId('setup-step-done').click();
		await page.getByTestId('setup-finish').click();
		await expect(page).toHaveURL(/\/$/);
		expect(s.puts.at(-1)).toMatchObject({ status: 'completed', step: 'done' });
	});

	test('addresses: detected values are shown and saved', async ({ page }) => {
		const s = await hub(page, { lang: 'en', setup: { status: 'in_progress', step: 'urls', skipped: [] }, haUrls: { ha_url: '', ha_internal_url: '', ha_url_detected: 'https://abc.ui.nabu.casa', ha_internal_url_detected: 'http://198.51.100.178' } });
		const requests = [];
		page.on('request', (r) => r.url().includes('/settings/ha-urls') && r.method() === 'PUT' && requests.push(r.postDataJSON()));
		await page.goto('/setup');
		await expect(panel(page, 'urls')).toContainText('detected: https://abc.ui.nabu.casa');
		await expect(panel(page, 'urls')).toContainText('Guests get it only if their profile allows it');
		await page.getByLabel('External address (optional)').fill('notaurl');
		await page.getByTestId('setup-save-urls').click();
		await expect(panel(page, 'urls').getByRole('alert')).toBeVisible();
		await page.getByLabel('External address (optional)').fill('https://home.example.com');
		await page.getByTestId('setup-save-urls').click();
		await expect.poll(() => requests.at(-1)).toEqual({ ha_url: 'https://home.example.com', ha_internal_url: null });
		expect(s.haUrls.ha_url).toBe('https://home.example.com');
	});

	test('first area: rooms from Home Assistant are offered and imported with their devices', async ({ page }) => {
		const s = await hub(page, { lang: 'de', setup: { status: 'in_progress', step: 'area', skipped: [] }, integration: CONNECTED });
		await page.goto('/setup');
		const chips = page.getByTestId('setup-ha-areas');
		await expect(chips.getByRole('button')).toHaveCount(7);
		await chips.getByRole('button', { name: 'Küche' }).click();
		await chips.getByRole('button', { name: 'Wohnzimmer' }).click();
		await expect(chips.getByRole('button', { name: 'Küche' })).toHaveAttribute('aria-pressed', 'true');
		await page.getByTestId('setup-import-areas').click();
		await expect(page.getByTestId('setup-area-ok')).toContainText('2 Bereiche angelegt');
		expect(s.syncs[0]).toEqual({ area_ids: ['kueche', 'wohnzimmer'], include_entities: true, domains: null });
		// Imported rooms are marked and cannot be imported twice.
		await expect(chips.getByRole('button', { name: /Küche/ })).toBeDisabled();
		await expect(page.getByTestId('setup-step-area')).toHaveAttribute('data-state', 'done');
	});

	test('first area: what the import left out is named', async ({ page }) => {
		// Like the areas page: every entry of result.errors is shown.
		const syncErrors = ["Area 'Küche': 12 entities not added (at most 20 sections of 50)"];
		await hub(page, { setup: { status: 'in_progress', step: 'area', skipped: [] }, integration: CONNECTED, syncErrors });
		await page.goto('/setup');
		await page.getByTestId('setup-ha-areas').getByRole('button', { name: 'Küche' }).click();
		await page.getByTestId('setup-import-areas').click();
		await expect(page.getByText(/1 item not taken over: .*12 entities not added/)).toBeVisible();
	});

	test('first area: an own empty area can be created', async ({ page }) => {
		const s = await hub(page, { lang: 'en', setup: { status: 'in_progress', step: 'area', skipped: [] } });
		await page.goto('/setup');
		await expect(page.getByTestId('setup-create-area')).toBeDisabled();
		await page.getByLabel('Create your own area').fill('Hobby room');
		await page.getByTestId('setup-create-area').click();
		await expect(page.getByTestId('setup-area-ok')).toBeVisible();
		expect(s.created[0]).toMatchObject({ name: 'Hobby room' });
	});

	test('first device: pairing opens the dialog and a new device shows up in the assistant', async ({ page }) => {
		const s = await hub(page, { lang: 'en', setup: { status: 'in_progress', step: 'device', skipped: [] }, integration: CONNECTED, areas: [AREA] });
		await page.goto('/setup');
		// The store link is a placeholder during the beta: no download button, no placeholder text.
		await expect(page.getByTestId('setup-app-store')).toHaveCount(0);
		await expect(page.locator('body')).not.toContainText('[[');
		await expect(page.locator('[data-placeholder]')).toHaveCount(0);
		await expect(page.getByText('apps for iPhone, iPad, Apple TV and Apple Watch are in beta; install them through TestFlight: https://testflight.apple.com/join/fHX7YT8v.', { exact: false })).toBeVisible();
		await page.getByTestId('setup-pair').click();
		const dialog = page.getByRole('dialog', { name: 'Pair device' });
		await expect(dialog).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(dialog).toBeHidden();

		s.clients = [DEVICE];
		await expect.poll(() => !!s.socket).toBe(true);
		s.socket.send(JSON.stringify({ type: 'client_paired', data: { client_id: DEVICE.id, client_name: DEVICE.name, is_guest: false } }));
		await expect(page.getByTestId('setup-devices')).toContainText('Owner B iPhone');
		await expect(page.getByTestId('setup-device-ok')).toBeVisible();
		await expect(page).toHaveURL(/\/setup$/);
	});

	test('Home Assistant older than 2025.11 is flagged', async ({ page }) => {
		await hub(page, { lang: 'en', integration: { ...CONNECTED, ha_version: '2025.5.3' } });
		await page.goto('/setup');
		await expect(page.getByText('Home Assistant 2025.5.3 is too old').first()).toBeVisible();
		await page.getByTestId('setup-step-connection').click();
		await expect(page.getByTestId('setup-step-connection')).toHaveAttribute('data-state', 'open');
	});

	test('does not open by itself when the hub is set up or the assistant was finished', async ({ page }) => {
		await hub(page, { lang: 'en', areas: [AREA], clients: [DEVICE], integration: CONNECTED });
		await page.goto('/');
		await expect(page.getByTestId('setup-checklist')).toBeVisible();
		await expect(page).toHaveURL(/\/$/);
		await page.getByTestId('open-setup').click();
		await expect(page).toHaveURL(/\/setup$/);
	});

	test('a finished assistant stays closed on an empty hub; the overview offers to resume an open one', async ({ page }) => {
		const s = await hub(page, { lang: 'de', setup: { status: 'completed', step: 'done', skipped: [] } });
		await page.goto('/');
		await expect(page.getByTestId('setup-checklist')).toBeVisible();
		await expect(page.getByTestId('open-setup')).toHaveText(/Assistent starten/);
		s.setup = { status: 'in_progress', step: 'area', skipped: [] };
		await page.reload();
		await expect(page.getByTestId('open-setup')).toHaveText(/Assistent fortsetzen/);
		await expect(page).toHaveURL(/\/$/);
	});

	for (const lang of ['de', 'en']) {
		for (const [vp, size] of [['desktop', { width: 1280, height: 860 }], ['mobile', { width: 390, height: 844 }]]) {
			test(`equal sizes: equal buttons and tiles (${lang}, ${vp})`, async ({ page }) => {
				await page.setViewportSize(size);
				await hub(page, { lang, setup: { status: 'in_progress', step: 'integration', skipped: [] } });
				await page.goto('/setup');
				await expect(panel(page, 'integration')).toBeVisible();
				const sameSize = async (locator, what) => {
					const boxes = await locator.evaluateAll((els) => els.map((e) => e.getBoundingClientRect()).map((r) => [Math.round(r.width), Math.round(r.height)]));
					expect(boxes.length, what).toBeGreaterThan(1);
					for (const b of boxes) expect(b, what).toEqual(boxes[0]);
				};
				await sameSize(page.getByTestId('setup-nav').getByRole('button'), 'wizard buttons');
				await sameSize(page.getByTestId('setup-steps').getByRole('button'), 'step buttons');
				await sameSize(panel(page, 'integration').locator('.eq > .btn'), 'integration actions');
				await page.getByTestId('setup-step-device').click();
				await sameSize(panel(page, 'device').locator('.tile'), 'device tiles');
				await page.getByTestId('setup-step-done').click();
				await sameSize(page.getByTestId('setup-nav').getByRole('button'), 'finish buttons');
				await sameSize(page.getByTestId('setup-next').locator('.nextcard'), 'next-step cards');
				const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
				expect(scroll).toBeLessThanOrEqual(0);
			});
		}
	}

	test('accessibility: named steps, current step and keyboard', async ({ page }) => {
		await hub(page, { lang: 'en' });
		await page.goto('/setup');
		const nav = page.getByRole('navigation', { name: 'Setup steps' });
		await expect(nav.getByRole('button', { name: '1. Integration, open', exact: true })).toHaveAttribute('aria-current', 'step');
		await page.getByTestId('setup-next-btn').focus();
		await page.keyboard.press('Enter');
		await expect(page.locator('#setup-step-title')).toBeFocused();
		await expect(page.locator('#setup-step-title')).toHaveText('Connection to Home Assistant');
		await expect(page.getByRole('progressbar', { name: 'Setup steps' })).toBeVisible();
	});
});
