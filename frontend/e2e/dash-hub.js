/**
 * Mocked hub for the overview (dashboard) tests: a fully set-up home (setup
 * checklist complete or not), devices, areas and the log of log-hub.js with
 * the important events of the card "Recent important" (alarms, kids'
 * requests, guests, new devices, profile changes).
 *
 *   const hub = await dashHub(page, { lang: 'de', complete: true });
 *   hub.push({ type: 'log_entry', data: entry });   // admin broadcast
 */
import { setupApiMocks } from './helpers.js';
import { createLogHub, importantOf, sampleEntries, DEVICES } from './log-hub.js';

const MIN = 60_000;
export const at = (minAgo) => new Date(Date.now() - minAgo * MIN).toISOString().replace(/\.\d+Z$/, '+00:00');

const [AIR, KID, GUEST, PANEL] = DEVICES.map((d) => d.id);

export const AREAS = ['Wohnzimmer', 'Küche', 'Schlafzimmer', 'Büro', 'Eingang', 'Garage', 'Garten'].map((name, i) => ({
	id: `a-${i}`,
	name,
	icon: 'mdi:sofa',
	is_active: i !== 6,
	sort_order: i,
	tags: [],
	entity_count: 3 + i,
}));

const client = (id, name, owner, o = {}) => ({
	id,
	name,
	owner_label: owner,
	is_active: true,
	is_connected: true,
	access: 'member',
	expires_at: null,
	ha_token_status: 'active',
	created_at: at(20 * 24 * 60),
	last_seen: at(1),
	platform: 'ios',
	...o,
});

export const CLIENTS = [
	client(AIR, 'iPhone Air', 'Owner A'),
	client(KID, 'iPad Kid', 'Kid', { platform: 'ipados', child_protection: true }),
	client(GUEST, 'Gast Visitor', 'Visitor', { access: 'guest', expires_at: at(-3 * 24 * 60), is_connected: false, last_seen: at(30) }),
	client(PANEL, 'iPad Küche', null, { platform: 'ipados' }),
];

const CONNECTED = { connected: true, push_connected: true, last_request_ts: Date.now() / 1000 - 12, request_count: 42, auth_failure_count: 0 };

function base(id, minAgo, o) {
	const dev = DEVICES.find((d) => d.id === o.client_id);
	const e = {
		id,
		ts: at(minAgo),
		category: 'activity',
		kind: 'control',
		event_kind: 'control',
		source: 'hub',
		client_id: null,
		device_name: dev?.name ?? null,
		owner_label: dev?.owner ?? null,
		profile_id: null,
		profile_name: null,
		area_id: null,
		area_name: null,
		entity_id: null,
		entity_name: null,
		action: null,
		route: null,
		origin: null,
		control_path: null,
		result: null,
		reason: null,
		count: null,
		since: null,
		until: null,
		actor: null,
		detail: null,
		device_exists: !!dev,
		profile_exists: false,
		unread: false,
		...o,
	};
	return { ...e, important: o.important ?? importantOf(e) };
}

/** The log of log-hub.js plus a kids' request and its answer. */
export function dashEntries() {
	return [
		...sampleEntries(),
		base(101, 20, { kind: 'kids_release_requested', event_kind: 'kids_release_requested', client_id: KID, entity_id: 'media_player.tv', entity_name: 'Fernseher', action: 'unlock_device', detail: { request_id: 'r1', category: 'unlock_device' } }),
		base(102, 120, { kind: 'kids_request_answered', event_kind: 'kids_request_answered', client_id: KID, action: 'yes', until: at(-30), detail: { request_id: 'r0', category: 'stay_up', by: { kind: 'admin', name: 'Owner A' } } }),
	];
}

/**
 * @param {import('@playwright/test').Page} page
 * @param {{ lang?: string, theme?: string, complete?: boolean, entries?: object[], setupOpen?: string|null }} [opts]
 */
export async function dashHub(page, { lang = 'de', theme = 'light', complete = true, entries = dashEntries(), setupOpen = null, failLog = false } = {}) {
	const log = createLogHub({ entries });
	const s = { log, socket: null, setup: { status: 'completed', step: 'done', skipped: [] }, logCalls: [] };
	await page.addInitScript(
		([l, th, so]) => {
			localStorage.setItem('hub.lang', l);
			localStorage.setItem('hub.theme', th);
			if (so !== null && !sessionStorage.getItem('dash-init')) {
				sessionStorage.setItem('dash-init', '1');
				if (so === '') localStorage.removeItem('hub.dash.setup');
				else localStorage.setItem('hub.dash.setup', so);
			}
		},
		[lang, theme, setupOpen],
	);
	const guestProfile = {
		id: 'p-guest',
		name: 'Gast',
		rules: { areas: complete ? { mode: 'all' } : { mode: 'list', area_ids: [], tags: [] } },
	};
	await setupApiMocks(page, {
		areas: AREAS,
		clients: CLIENTS,
		ws: (ws) => {
			s.socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
		handler: async (ctx) => {
			const { path, url, json } = ctx;
			if (path === '/admin/integration-status') return json(CONNECTED);
			if (path === '/admin/setup-assistant') return json(s.setup);
			if (path === '/settings/profiles') return json({ default_profiles: { guest: 'p-guest', member: 'p-adult' } });
			if (path === '/profiles' && ctx.method === 'GET') return json({ profiles: [guestProfile] });
			if (path === '/log' && ctx.method === 'GET') {
				s.logCalls.push(url.searchParams.toString());
				if (failLog) return json({ detail: 'boom' }, 500);
				const kind = url.searchParams.get('kind');
				if (kind) {
					// log-hub.js knows no kind filter: answer it here.
					const limit = Number(url.searchParams.get('limit') || 100);
					const week = Date.now() - 7 * 24 * 60 * MIN;
					const list = log.state.entries
						.filter((e) => e.kind === kind && (url.searchParams.get('range') !== '7d' || Date.parse(e.ts) >= week))
						.sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts) || b.id - a.id);
					return json({ entries: list.slice(0, limit), has_more: list.length > limit });
				}
			}
			return log.handler(ctx);
		},
	});
	s.push = (msg) => s.socket?.send(JSON.stringify(msg));
	return s;
}

export { DEVICES, base as logEntry };
