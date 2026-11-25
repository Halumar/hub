/**
 * In-memory hub for the log tab used by the e2e tests: GET /log with
 * the filters of api/log.py, summary, read marks, settings, export,
 * notification rules, notify targets and the test send.
 *
 *   const hub = createLogHub();
 *   await setupApiMocks(page, { handler: hub.handler });
 */
import { readFileSync } from 'node:fs';

const MIN = 60_000;
const at = (minAgo) => new Date(Date.now() - minAgo * MIN).toISOString().replace(/\.\d+Z$/, '+00:00');

export const DEVICES = [
	{ id: '11111111-1111-4111-8111-111111111111', name: 'iPhone Air', owner: 'Owner A', profile: 'p-adult' },
	{ id: '22222222-2222-4222-8222-222222222222', name: 'iPad Kid', owner: 'Kid', profile: 'p-kid' },
	{ id: '33333333-3333-4333-8333-333333333333', name: 'Gast Visitor', owner: 'Visitor', profile: 'p-guest' },
	{ id: '44444444-4444-4444-8444-444444444444', name: 'iPad Küche', owner: null, profile: 'p-panel' },
];
const PROFILES = { 'p-adult': 'Erwachsene Person', 'p-kid': 'Kind', 'p-guest': 'Gast', 'p-panel': 'Wandpanel' };

/** Kinds marked important in the hub's copy of the contract taxonomy. */
const IMPORTANT_KINDS = new Set(
	JSON.parse(readFileSync(new URL('../../halumar/rootfs/opt/hub/data/events.json', import.meta.url), 'utf8'))
		.kinds.filter((k) => k.important)
		.map((k) => k.kind),
);

/** Entry field `important` like services/event_log.is_important. */
export function importantOf(e) {
	return e.category === 'alarm' || IMPORTANT_KINDS.has(e.event_kind) || IMPORTANT_KINDS.has(e.kind);
}
const AREAS = [
	{ id: 'a-kitchen', name: 'Küche' },
	{ id: 'a-garage', name: 'Garage' },
	{ id: 'a-kids', name: 'Kinderzimmer' },
];

function entry(id, minAgo, o) {
	const dev = DEVICES.find((d) => d.id === o.client_id);
	const e = {
		id,
		ts: at(minAgo),
		category: 'activity',
		kind: 'control',
		event_kind: 'control',
		source: 'integration',
		client_id: null,
		device_name: dev?.name ?? null,
		owner_label: dev?.owner ?? null,
		profile_id: dev?.profile ?? null,
		profile_name: dev ? PROFILES[dev.profile] : null,
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
		profile_exists: !!dev,
		unread: false,
		...o,
	};
	return { ...e, important: o.important ?? importantOf(e) };
}

export function sampleEntries() {
	const [air, kid, visitor, panel] = DEVICES.map((d) => d.id);
	return [
		entry(1, 4, { client_id: air, entity_id: 'light.kitchen', entity_name: 'Küchenlicht', area_id: 'a-kitchen', area_name: 'Küche', action: 'turn_on', control_path: 'direct', source: 'ha', result: 'ok' }),
		entry(2, 9, { category: 'alarm', kind: 'denied', event_kind: 'denied', source: 'hub', client_id: kid, entity_id: 'lock.front_door', entity_name: 'Haustür', action: 'unlock', reason: 'blocked_area', count: 3, since: at(17), detail: { derived: true, window_minutes: 10, entities: ['lock.front_door'] }, unread: true }),
		entry(3, 10, { client_id: kid, entity_id: 'lock.front_door', entity_name: 'Haustür', action: 'unlock', route: 'local', origin: 'app', control_path: 'checked', result: 'denied', kind: 'denied', event_kind: 'denied', reason: 'blocked_area', detail: { sent: [{ target: 'notify.mobile_app_owner_a', status: 'handed_over' }, { target: air, name: 'iPhone Air', status: 'waiting_for_app' }] } }),
		entry(4, 35, { client_id: visitor, entity_id: 'cover.garage_door', entity_name: 'Garagentor', area_id: 'a-garage', area_name: 'Garage', action: 'open_cover', route: 'external', origin: 'quick_access', control_path: 'checked', result: 'denied', kind: 'denied', event_kind: 'denied', reason: 'blocked_view_only' }),
		entry(5, 60, { category: 'alarm', kind: 'access_group_restored', event_kind: 'access_group_restored', client_id: visitor, detail: { ha_group: 'read_only' }, unread: true }),
		entry(6, 90, { category: 'change', kind: 'override_set', event_kind: null, source: 'hub', client_id: kid, actor: 'Owner A', detail: { rule: 'kids_volume_max', before: null, after: { value: 70, expires_at: at(-600) } } }),
		entry(7, 26 * 60, { category: 'change', kind: 'guest_paired', event_kind: 'guest_paired', source: 'hub', client_id: visitor, until: at(-3 * 24 * 60) }),
		entry(8, 27 * 60, { category: 'change', kind: 'profile_updated', event_kind: 'profile_changed', source: 'hub', actor: 'Owner A', profile_id: 'p-kid', profile_name: 'Kind', device_name: null, owner_label: null, detail: { before: { name: 'Kind', bedtime: '20:00' }, after: { name: 'Kind', bedtime: '20:30' } } }),
		entry(9, 3 * 24 * 60, { category: 'alarm', kind: 'integration_outage', event_kind: 'integration_outage', source: 'hub', since: at(3 * 24 * 60 + 17), until: at(3 * 24 * 60), detail: { live: true } }),
		entry(10, 3 * 24 * 60 + 30, { client_id: panel, entity_id: 'light.kitchen', entity_name: 'Küchenlicht', area_id: 'a-kitchen', area_name: 'Küche', action: 'turn_off', route: 'local', origin: 'app', control_path: 'checked', result: 'ok' }),
		entry(11, 9 * 24 * 60, { category: 'change', kind: 'device_paired', event_kind: 'device_paired', source: 'hub', client_id: panel, actor: 'Owner A' }),
		entry(12, 12 * 24 * 60, { client_id: kid, entity_id: 'light.kids', entity_name: 'Licht Kinderzimmer', area_id: 'a-kids', area_name: 'Kinderzimmer', action: 'turn_on', route: 'local', control_path: 'hub', kind: 'kids_control', event_kind: 'kids_control', source: 'hub', result: 'ok' }),
	];
}

export function createLogHub({ entries = sampleEntries(), integration = true, notify = null } = {}) {
	const state = {
		entries: entries.map((e) => ({ ...e })),
		settings: { retention_days: 30, hidden_owners: [], alarm_threshold: { count: 3, window_minutes: 10 } },
		config: { retention_days: 30, quiet_default: { start: '22:00', end: '07:00' }, rules: [] },
		targets: {
			notify: notify ?? [
				{ service: 'notify.mobile_app_owner_a', name: 'Mobile App Owner A', description: '' },
				{ service: 'notify.persistent_notification', name: 'Persistent notification', description: '' },
				{ service: 'notify.telegram', name: 'Telegram', description: '' },
			],
			devices: [
				{ client_id: DEVICES[2].id, name: 'Gast Visitor', owner_label: 'Visitor', platform: 'ios', kind: 'guest', expires_at: at(-3 * 24 * 60) },
				{ client_id: DEVICES[1].id, name: 'iPad Kid', owner_label: 'Kid', platform: 'ipados', kind: 'child', expires_at: null },
				{ client_id: DEVICES[3].id, name: 'iPad Küche', owner_label: null, platform: 'ipados', kind: 'member', expires_at: null },
				{ client_id: DEVICES[0].id, name: 'iPhone Air', owner_label: 'Owner A', platform: 'ios', kind: 'member', expires_at: null },
			],
			error: null,
		},
		requests: [],
		tests: [],
	};

	function visible() {
		return state.entries.filter((e) => !(e.category === 'activity' && e.owner_label && state.settings.hidden_owners.includes(e.owner_label)));
	}

	function filtered(p) {
		const now = Date.now();
		const q = (p.get('q') || '').toLowerCase();
		return visible()
			.filter((e) => !p.get('category') || e.category === p.get('category'))
			.filter((e) => {
				const r = p.get('range');
				if (r === 'today') return new Date(e.ts).toDateString() === new Date().toDateString();
				if (r === '7d') return now - Date.parse(e.ts) <= 7 * 24 * 60 * MIN;
				return true;
			})
			.filter((e) => !p.get('client_id') || e.client_id === p.get('client_id'))
			.filter((e) => !p.get('owner') || e.owner_label === p.get('owner'))
			.filter((e) => !p.get('profile_id') || e.profile_id === p.get('profile_id'))
			.filter((e) => !p.get('area_id') || e.area_id === p.get('area_id'))
			.filter((e) => p.get('important') !== 'true' || e.important)
			.filter((e) => !q || JSON.stringify(e).toLowerCase().includes(q))
			.sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts) || b.id - a.id);
	}

	function summary() {
		const v = visible();
		const week = Date.now() - 7 * 24 * 60 * MIN;
		const unread = v.filter((e) => e.unread);
		return {
			namespace: 'halumar',
			unread_alarms: unread.length,
			unread: unread.map((e) => ({ id: e.id, ts: e.ts, kind: e.kind, device_name: e.device_name, client_id: e.client_id })),
			counts: {
				activity_today: v.filter((e) => e.category === 'activity' && new Date(e.ts).toDateString() === new Date().toDateString()).length,
				changes_7d: v.filter((e) => e.category === 'change' && Date.parse(e.ts) >= week).length,
				alarms_7d: v.filter((e) => e.category === 'alarm' && Date.parse(e.ts) >= week).length,
			},
			total: v.length,
			oldest_ts: v.length ? v.map((e) => e.ts).sort()[0] : null,
			retention_days: state.settings.retention_days,
			hidden_owners: state.settings.hidden_owners,
			integration_connected: integration,
			time_zone: 'Europe/Zurich',
			facets: {
				devices: DEVICES.map((d) => ({ id: d.id, name: d.name })),
				owners: ['Owner A', 'Visitor', 'Kid'],
				profiles: Object.entries(PROFILES).map(([id, name]) => ({ id, name })),
				areas: AREAS,
			},
		};
	}

	async function handler({ path, method, request, url, json }) {
		const body = () => request.postDataJSON?.() || {};
		if (path === '/log' && method === 'GET') {
			state.requests.push(`GET /log?${url.searchParams}`);
			let list = filtered(url.searchParams);
			const before = Number(url.searchParams.get('before_id'));
			if (before) {
				const anchor = state.entries.find((e) => e.id === before);
				list = list.filter((e) => Date.parse(e.ts) < Date.parse(anchor.ts));
			}
			const limit = Number(url.searchParams.get('limit') || 100);
			return json({ entries: list.slice(0, limit), has_more: list.length > limit });
		}
		if (path === '/log/export') return json({ entries: filtered(url.searchParams) });
		if (path === '/log/summary') return json(summary());
		if (path === '/log/read' && method === 'POST') {
			const b = body();
			state.requests.push(`POST /log/read ${JSON.stringify(b)}`);
			let n = 0;
			for (const e of state.entries) if (e.unread && (b.all || b.ids?.includes(e.id))) ((e.unread = false), n++);
			return json({ marked: n });
		}
		if (path === '/log/settings' && method === 'GET') return json(state.settings);
		if (path === '/log/settings' && method === 'PUT') {
			state.settings = body();
			state.config.retention_days = state.settings.retention_days;
			state.requests.push(`PUT /log/settings ${JSON.stringify(state.settings)}`);
			return json(state.settings);
		}
		if (path === '/log/notify-targets') return json(state.targets);
		if (path === '/settings/event-notifications' && method === 'GET') return json(state.config);
		if (path === '/settings/event-notifications' && method === 'PUT') {
			const b = body();
			for (const r of b.rules) if (r.enabled && !r.targets.notify.length && !r.targets.devices.length) return json({ detail: { code: 'notifications.no_target' } }, 422);
			state.config = b;
			state.requests.push(`PUT /settings/event-notifications ${JSON.stringify(b)}`);
			return json(b);
		}
		if (path === '/settings/event-notifications/test' && method === 'POST') {
			const b = body();
			state.tests.push(b);
			const rule = b.rule;
			return json({
				results: [
					...rule.targets.notify.map((x) => ({ target: x, status: 'handed_over' })),
					...rule.targets.devices.map((x) => ({ target: x, name: state.targets.devices.find((d) => d.client_id === x)?.name, status: 'waiting_for_app' })),
				],
				ha_event: 'handed_over',
			});
		}
		if (path === '/ha/entities')
			return json([
				{ entity_id: 'lock.front_door', friendly_name: 'Haustür', domain: 'lock' },
				{ entity_id: 'cover.garage_door', friendly_name: 'Garagentor', domain: 'cover' },
				{ entity_id: 'alarm_control_panel.security', friendly_name: 'Alarmanlage', domain: 'alarm_control_panel' },
				{ entity_id: 'light.kitchen', friendly_name: 'Küchenlicht', domain: 'light' },
			]);
		return undefined;
	}

	return { state, handler };
}
