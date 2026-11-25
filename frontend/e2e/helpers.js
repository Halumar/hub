/**
 * Shared test helpers: mocked API layer.
 *
 * Every E2E test registers these route handlers so the SvelteKit app talks to
 * an in-memory fake instead of the real Halumar add-on backend. This keeps the
 * suite hermetic and CI-friendly (no Python backend / Home Assistant needed).
 *
 * Usage:
 *   await setupApiMocks(page, { areas: [...], notifications: [...] });
 *   await page.goto('/areas');
 *
 * Options beyond the datasets:
 *   handler(ctx)  per-test override; return a response (via ctx.json) or
 *                 undefined to fall through to the default fixtures.
 *   ws            'admin' (default: auth_ok as ingress), 'auth_required'
 *                 (external session the server does not authenticate) or a
 *                 function(ws) that drives the mocked socket itself.
 *   ingress       when set, window.__INGRESS_PATH is injected (HA Ingress).
 *   requests      array that collects "METHOD /path" of every API call.
 */

/** Default fixtures returned by the mocked endpoints. */
/** Answer of GET /api/areas/{id}/room-climate: automatic, nothing suitable placed. */
export const emptyRoomClimate = {
	temperature: { entity_id: null, pick: { source: 'auto', entity_ids: [] }, value: null, unit: null, options: [] },
	humidity: { entity_id: null, pick: { source: 'auto', entity_ids: [] }, value: null, unit: null, options: [] },
};

export const defaultData = {
	roomClimate: emptyRoomClimate,
	session: { authenticated: true, username: 'tester' },
	health: { status: 'ok', version: '2026.12.0' },
	areas: [
		{
			id: 'area-living-room',
			name: 'Living Room',
			icon: 'mdi:sofa',
			description: 'Main lounge',
			color: '#4361ee',
			grid_cols: 2,
			is_active: true,
			sort_order: 0,
			tags: ['ground_floor'],
			entity_count: 3,
			control_mode: null,
		},
		{
			id: 'area-kitchen',
			name: 'Kitchen',
			icon: 'mdi:fridge',
			description: 'Cooking area',
			color: null,
			grid_cols: 3,
			is_active: true,
			sort_order: 1,
			tags: ['ground_floor'],
			entity_count: 5,
			control_mode: 'basic',
		},
	],
	clients: [
		{
			id: 'client-1234abcd',
			name: 'iPad Hallway',
			is_active: true,
			is_connected: true,
			access: 'member',
			expires_at: null,
			profile_id: 'p-wall',
			profile_name: 'Wandpanel',
			child_protection: false,
			own_area_id: null,
			override_count: 0,
			temporary_override_count: 0,
			has_remote_pin: false,
			created_at: '2026-11-01T10:00:00Z',
			last_seen: '2026-11-18T10:00:00Z',
			has_ha_token: true,
			ha_user_id: 'ha-user-1',
			ha_token_status: 'active',
			ha_token_pending_since: null,
			owner_label: null,
			remote_allowed: false,
			platform: 'ipados',
			app_version: '2026.12.0',
			app_build: '6',
			device_model: 'iPad16,3',
			protocol_version: 1,
			update_required: false,
		},
	],
	notifications: [],
};

/** A detailed area (GET /areas/{id}) with a few sections for the editor. */
export function areaDetail(id = 'area-living-room', overrides = {}) {
	const base = defaultData.areas.find((a) => a.id === id) || defaultData.areas[0];
	const sections = [];
	for (let s = 0; s < 4; s++) {
		const entities = [];
		for (let e = 0; e < 4; e++) {
			entities.push({
				entity_id: `light.s${s}_lamp_${e}`,
				display_name: `Lamp ${s}.${e}`,
				columns: 1,
				rows: 1,
				is_visible: true,
				control_mode: null,
				sort_order: e,
			});
		}
		sections.push({
			id: `sec-${s}`,
			title: `Section ${s + 1}`,
			columns: 2,
			column_span: 1,
			sort_order: s,
			entities,
			widgets: [],
		});
	}
	return { ...base, sections, ...overrides };
}

/** Full client object as returned by GET /pairing/clients/{id}. */
export function clientDetail(overrides = {}) {
	return {
		...defaultData.clients[0],
		...overrides,
	};
}

/**
 * Register catch-all interception for /api/*. Known endpoints get sensible
 * fixtures; everything else returns an empty 200 so stray calls never hit the
 * network. Pass `overrides` to customize individual datasets per test.
 *
 * @param {import('@playwright/test').Page} page
 * @param {Partial<typeof defaultData> & { handler?: Function, ws?: any, ingress?: string, requests?: string[] }} [overrides]
 */
export async function setupApiMocks(page, overrides = {}) {
	const { handler, ws: wsMode = 'admin', ingress, requests, ...datasets } = overrides;
	const data = { ...defaultData, ...datasets };

	if (ingress) {
		await page.addInitScript((p) => {
			window.__INGRESS_PATH = p;
		}, ingress);
	}

	await page.route('**/api/**', async (route) => {
		const request = route.request();
		const method = request.method();
		const url = new URL(request.url());
		const path = url.pathname.replace(/^.*\/api/, ''); // strip any prefix -> "/areas"
		if (path === '/ws') return route.fallback();
		requests?.push(`${method} ${path}`);

		// Resolve to true so per-test handlers can `return json(...)`.
		const fulfill = (opts) => route.fulfill(opts).then(() => true);
		const json = (body, status = 200) =>
			fulfill({
				status,
				contentType: 'application/json',
				body: JSON.stringify(body),
			});

		if (handler) {
			const handled = await handler({ path, method, request, url, json, fulfill, data });
			if (handled !== undefined) return handled;
		}

		// --- Auth / health -------------------------------------------------
		if (path === '/auth/session') return json(data.session);
		if (path === '/health') return json(data.health);
		if (path === '/auth/login' && method === 'POST')
			return json({ success: true, username: data.session.username });
		if (path === '/auth/logout') return json({ success: true });

		// --- Areas ---------------------------------------------------------
		if (path === '/areas' && method === 'GET') return json(data.areas);
		if (path === '/areas' && method === 'POST') {
			const payload = request.postDataJSON?.() || {};
			return json({ id: 'area-new', entity_count: 0, ...payload }, 201);
		}
		if (/^\/areas\/[^/]+\/room-climate$/.test(path)) return json(data.roomClimate);
		const areaMatch = path.match(/^\/areas\/([^/]+)$/);
		if (areaMatch && method === 'GET') return json(areaDetail(areaMatch[1]));
		if (path.startsWith('/areas/') && method === 'PATCH') return json({ success: true });
		if (path.startsWith('/areas/') && method === 'PUT') return json({ success: true });

		// --- Clients / pairing --------------------------------------------
		if (path === '/pairing/clients') return json(data.clients);
		const clientMatch = path.match(/^\/pairing\/clients\/([^/]+)$/);
		if (clientMatch && method === 'GET') return json(clientDetail({ id: clientMatch[1] }));
		if (path === '/pairing/status') return json({ pairing_active: false });

		// --- Notifications -------------------------------------------------
		if (path.startsWith('/notifications')) return json(data.notifications);

		// --- Fallback: empty success --------------------------------------
		return json({});
	});

	// Stub the WebSocket so no real backend is needed. Available in
	// Playwright >= 1.48.
	if (typeof page.routeWebSocket === 'function') {
		await page.routeWebSocket('**/api/ws', (ws) => {
			if (typeof wsMode === 'function') return wsMode(ws);
			if (wsMode === 'auth_required') {
				ws.send(JSON.stringify({ type: 'auth_required', message: 'Send auth with secret or pair with PIN', timeout: 30 }));
				return;
			}
			if (wsMode === 'silent') return;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		});
	}
}
