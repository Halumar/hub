/**
 * Rows of "Notifications at events" and their mapping to the hub's
 * rules (GET/PUT /api/settings/event-notifications, contract draft
 * rest/event-notifications.json): one rule per row with a fixed id.
 */
import { t } from '$lib/i18n/index.svelte.js';

export const GROUPS = [
	{ key: 'devices', icon: 'iphone' },
	{ key: 'guests', icon: 'users' },
	{ key: 'kids', icon: 'child', help: true },
	{ key: 'activity', icon: 'power' },
	{ key: 'alarms', icon: 'warn', help: true },
];

const ALL4 = ['person', 'device', 'profile', 'area'];

/** thr: [option key, rule fields]. */
export const ROWS = [
	{ key: 'dev_paired', group: 'devices', kinds: ['device_paired'], f: ['person', 'profile'] },
	{ key: 'dev_set_up', group: 'devices', kinds: ['device_set_up'], f: ['person', 'profile'], help: true },
	{ key: 'dev_removed', group: 'devices', kinds: ['device_removed'], f: ['person', 'profile'] },
	// Idn keeps the rule ids of the other rows stable.
	{ key: 'dev_access', idn: 102, group: 'devices', kinds: ['access_opened', 'access_closed'], f: ['person', 'device', 'profile'], help: true },
	// Early access requests
	{ key: 'dev_access_request', idn: 103, group: 'devices', kinds: ['access_requested'], f: ['person', 'device', 'profile'], help: true },
	{ key: 'dev_access_answered', idn: 104, group: 'devices', kinds: ['access_request_answered', 'access_request_expired'], f: ['person', 'device', 'profile'] },
	{ key: 'dev_profile', group: 'devices', kinds: ['profile_changed'], f: ['profile', 'device'], help: true },
	{
		key: 'dev_adjustment',
		group: 'devices',
		kinds: ['adjustment_expired'],
		f: ['device', 'profile'],
		timing: true,
		thr: [
			['atEnd', { kinds: ['adjustment_expired'] }],
			['before1h', { kinds: ['adjustment_expiring'], lead_minutes: 60 }],
		],
	},
	{ key: 'guest_invited', group: 'guests', kinds: ['guest_invited'], f: ['profile'] },
	{ key: 'guest_paired', group: 'guests', kinds: ['guest_paired'], f: ['profile'], help: true },
	{
		key: 'guest_soon',
		group: 'guests',
		kinds: ['guest_expiring'],
		f: ['profile'],
		timing: true,
		thr: [
			['before24h', { lead_minutes: 1440 }],
			['before3h', { lead_minutes: 180 }],
			['before1h', { lead_minutes: 60 }],
		],
	},
	{ key: 'guest_expired', group: 'guests', kinds: ['guest_expired'], f: ['profile'] },
	{ key: 'kids_control', group: 'kids', kinds: ['kids_control'], f: ['person', 'device', 'area'], bundleExample: true },
	{ key: 'kids_bedtime', group: 'kids', kinds: ['kids_bedtime_attempt'], f: ['person', 'device'] },
	{ key: 'kids_pause', group: 'kids', kinds: ['kids_pause'], f: ['person', 'device'] },
	// Idn keeps the rule ids of the rows listed before it stable.
	{ key: 'kids_paused_all', idn: 101, group: 'kids', kinds: ['kids_paused_all'], f: ['person', 'device'], help: true },
	{ key: 'kids_ask', group: 'kids', kinds: ['kids_release_requested'], f: ['person', 'device'] },
	{ key: 'kids_answered', group: 'kids', kinds: ['kids_request_answered', 'kids_request_expired'], f: ['person', 'device'] },
	{ key: 'family_change', group: 'kids', kinds: ['family_change'], f: ['person', 'device'] },
	{ key: 'act_away', group: 'activity', kinds: ['control', 'kids_control'], f: ALL4, routes: ['external'] },
	{ key: 'act_entities', group: 'activity', kinds: ['control', 'kids_control'], f: ['person', 'device', 'profile'], entities: true },
	{
		key: 'alarm_denied',
		group: 'alarms',
		kinds: ['denied'],
		f: ALL4,
		help: true,
		thr: [
			['every', { threshold: { count: 1, window_minutes: 10 } }],
			['denied3', { threshold: { count: 3, window_minutes: 10 } }],
			['denied5', { threshold: { count: 5, window_minutes: 10 } }],
		],
		thrDefault: 'denied3',
	},
	{
		key: 'alarm_access',
		group: 'alarms',
		kinds: ['access_group_restored', 'direct_access_attempt'],
		f: ['device', 'profile'],
		help: true,
		thr: [
			['now', { threshold: { count: 1, window_minutes: 10 } }],
			['access3', { threshold: { count: 3, window_minutes: 10 } }],
			['access10', { threshold: { count: 10, window_minutes: 10 } }],
		],
	},
	{
		key: 'alarm_outage',
		group: 'alarms',
		kinds: ['integration_outage'],
		f: [],
		help: true,
		back: true,
		thr: [
			['after1', { delay_minutes: 1 }],
			['after5', { delay_minutes: 5 }],
			['after15', { delay_minutes: 15 }],
		],
		thrDefault: 'after5',
	},
];

export const ROW = Object.fromEntries(ROWS.map((r) => [r.key, r]));

/** Fixed rule id per row (uuid shape): position among the original rows, or idn. */
export function ruleId(key) {
	const i = ROW[key]?.idn ?? ROWS.filter((r) => !r.idn).findIndex((r) => r.key === key) + 1;
	return `10a0e0e0-0000-4000-8000-${String(i).padStart(12, '0')}`;
}

export const TEMPLATES = [
	{ key: 'security', icon: 'shield', chip: 'c-warn', rows: ['alarm_denied', 'alarm_access', 'alarm_outage'], quiet: 'always' },
	{ key: 'parents', icon: 'child', chip: 'c-light', rows: ['kids_control', 'kids_bedtime', 'kids_pause', 'kids_paused_all', 'kids_ask'], bundle: { kids_control: 10, kids_bedtime: 10 } },
	{ key: 'guests', icon: 'users', chip: 'c-violet', rows: ['guest_invited', 'guest_paired', 'guest_soon', 'guest_expired'], quiet: 'night' },
	{ key: 'away', icon: 'globe', chip: 'c-info', rows: ['act_away', 'act_entities'], bundle: { act_away: 10 }, entities: true },
];

export function freshRow(row) {
	return {
		on: false,
		notify: [],
		devices: [],
		f: { person: '', device: '', profile: '', area: '' },
		thr: row.thr ? row.thrDefault || row.thr[0][0] : null,
		quiet: 'always',
		bundle: 0,
		ents: [],
		back: false,
	};
}

/** UI state of every row from the stored rules. */
export function rowsFromRules(rules) {
	const byId = Object.fromEntries((rules || []).map((r) => [r.id, r]));
	const out = {};
	for (const row of ROWS) {
		const rule = byId[ruleId(row.key)];
		const s = freshRow(row);
		if (rule) {
			s.on = !!rule.enabled;
			s.notify = [...rule.targets.notify];
			s.devices = [...rule.targets.devices];
			s.f = {
				person: rule.filters.owner_labels[0] || '',
				device: rule.filters.client_ids[0] || '',
				profile: rule.filters.profile_ids[0] || '',
				area: rule.filters.area_ids[0] || '',
			};
			s.ents = [...rule.filters.entity_ids];
			s.quiet = rule.quiet_hours?.length ? 'night' : 'always';
			s.bundle = rule.bundle_minutes || 0;
			s.back = rule.kinds.includes('integration_started');
			if (row.thr) {
				const match = row.thr.find(([, fields]) =>
					Object.entries(fields).every(([k, v]) => JSON.stringify(k === 'kinds' ? rule.kinds : rule[k]) === JSON.stringify(v)),
				);
				if (match) s.thr = match[0];
			}
		}
		out[row.key] = s;
	}
	return out;
}

/** The rule of one row (only rows that are on or have targets are stored). */
export function ruleFromRow(row, s, quietDefault) {
	const thrFields = row.thr ? Object.fromEntries(row.thr)[s.thr] || {} : {};
	let kinds = thrFields.kinds || row.kinds;
	if (row.back && s.back) kinds = [...kinds, 'integration_started'];
	const rule = {
		id: ruleId(row.key),
		name: t(`lognotify.rows.${row.key}`).slice(0, 60),
		enabled: s.on,
		kinds,
		targets: { notify: [...s.notify], devices: [...s.devices] },
		filters: {
			client_ids: s.f.device ? [s.f.device] : [],
			owner_labels: s.f.person ? [s.f.person] : [],
			profile_ids: s.f.profile ? [s.f.profile] : [],
			area_ids: s.f.area ? [s.f.area] : [],
			entity_ids: row.entities ? [...s.ents] : [],
			routes: row.routes ? [...row.routes] : [],
		},
		threshold: thrFields.threshold || { count: 1, window_minutes: 1 },
		bundle_minutes: s.bundle,
		quiet_hours: s.quiet === 'night' ? [{ days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], start: quietDefault.start, end: quietDefault.end }] : [],
		lead_minutes: thrFields.lead_minutes ?? null,
		delay_minutes: thrFields.delay_minutes ?? null,
	};
	return rule;
}

export function rulesFromRows(state, quietDefault, keepOther = []) {
	const ours = new Set(ROWS.map((r) => ruleId(r.key)));
	const rules = ROWS.filter((r) => state[r.key].on || state[r.key].notify.length || state[r.key].devices.length).map((r) =>
		ruleFromRow(r, state[r.key], quietDefault),
	);
	// Rules written by others (API) are kept unchanged.
	return [...rules, ...keepOther.filter((r) => !ours.has(r.id))];
}
