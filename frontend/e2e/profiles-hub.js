/**
 * In-memory hub for the profile model used by the e2e tests.
 *
 * Built against the admin API of the contract
 * (openapi/hub.openapi.json, tag profiles). The catalog and the
 * suggestions are copies of the contract data (fixtures/profiles/*.json); the
 * resolver is a JS port of tools/profiles_ref.py resolve(), so the device
 * page and the previews show realistic values without the Python hub.
 *
 *   const hub = createProfilesHub({ clients: [...] });
 *   await setupApiMocks(page, { handler: hub.handler, areas: hub.areas });
 *
 * hub.requests collects [method, path, body] of every profile write; with
 * E2E_REQUEST_LOG=<file> they are also appended there (JSON lines) so the
 * request bodies can be validated against the contract schemas.
 */
import fs from 'node:fs';

const read = (name) => JSON.parse(fs.readFileSync(new URL(`./fixtures/profiles/${name}`, import.meta.url), 'utf8'));
export const CATALOG = read('profile-rules.json');
export const SUGGESTIONS = read('profile-suggestions.json');

const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const NOW = () => new Date().toISOString();

export function same(a, b) {
	return JSON.stringify(sortKeys(a)) === JSON.stringify(sortKeys(b));
}
function sortKeys(v) {
	if (Array.isArray(v)) return v.map(sortKeys);
	if (v && typeof v === 'object') return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])]));
	return v;
}

/** Profile body of a new profile: empty or a copy of a suggestion (materialize). */
export function materialize(suggestion = null) {
	const rules = Object.fromEntries(CATALOG.rules.map((r) => [r.key, clone(r.default)]));
	const always = CATALOG.rules.filter((r) => !r.fixed_checkbox).map((r) => r.key);
	const allowedUc = new Set(CATALOG.rules.filter((r) => r.user_choice_allowed).map((r) => r.key));
	let fixed, uc, child, kind;
	if (!suggestion) {
		fixed = CATALOG.rules.filter((r) => r.default_fixed).map((r) => r.key);
		uc = CATALOG.rules.filter((r) => r.default_user_choice).map((r) => r.key);
		child = false;
		kind = 'person';
	} else {
		Object.assign(rules, clone(suggestion.rules));
		fixed = suggestion.fixed;
		uc = suggestion.user_choice;
		child = suggestion.child_protection;
		kind = suggestion.kind;
	}
	return { kind, child_protection: child, rules, fixed: [...new Set([...fixed, ...always])].sort(), user_choice: uc.filter((k) => allowedUc.has(k)).sort() };
}

// ── resolver (port of tools/profiles_ref.py) ─────────────────────────────────

const BOUNDARY_ORDER = ['guest', 'child_protection', 'hub_only'];
/** Platforms whose apps always hold their own key: "hub" resolves to "checked" there. */
const KEY_PLATFORMS = ['tvos', 'android_tv', 'watchos', 'wearos'];

const CONTROL_PATHS = ['hub', 'checked', 'direct'];

function normalize(key, v) {
	// An unknown control path counts as hub (fail closed).
	if (key === 'control_path' && !CONTROL_PATHS.includes(v)) return 'hub';
	if (key === 'areas' && v && typeof v === 'object') return { mode: v.mode, area_ids: [...(v.area_ids || [])], tags: [...(v.tags || [])], own_area: !!v.own_area };
	if (key === 'panel_start' && v && typeof v === 'object') return { mode: v.mode, area_id: v.mode === 'area' ? v.area_id ?? null : null };
	return v;
}

function resolveOwnArea(key, value, own) {
	let v = normalize(key, clone(value));
	if (key === 'areas') {
		if (v.own_area) {
			if (own && !v.area_ids.includes(own)) v.area_ids.push(own);
			v.own_area = false;
		}
		if (v.mode === 'all') {
			v.area_ids = [];
			v.tags = [];
		}
	} else if (key === 'panel_start') {
		if (v.mode === 'own_area') v = own ? { mode: 'area', area_id: own } : { mode: 'overview', area_id: null };
	} else if (key === 'kids_exceptions') {
		v = v.filter((e) => !e.target.own_area || own).map((e) => (e.target.own_area ? { target: { area_id: own }, rules: e.rules } : e));
	}
	return v;
}

const idx = (rule, v) => rule.order.findIndex((x) => same(x, v));
const entry = (value, source, reason, { uc = false, fixed = false, expires_at = null, then = null } = {}) => ({ value, source, reason, locked: !uc, fixed, user_choice: uc, expires_at, then });

function clamp(rule, e, { access, profile, device, expired, resolved = {} }) {
	let out = { ...e };
	// hub_only: active unless control_path (resolved first, catalog order) is checked or direct.
	const path = resolved.control_path?.value;
	const active = BOUNDARY_ORDER.filter((b) => (b === 'guest' && access.kind === 'guest') || (b === 'child_protection' && profile.child_protection) || (b === 'hub_only' && path !== 'checked' && path !== 'direct'));
	const by = Object.fromEntries(rule.boundaries.map((b) => [b.boundary, b]));
	for (const name of active) {
		const b = by[name];
		if (!b) continue;
		// lifted_by (catalog 9): parental controls do not cap watch while kids_watch is on.
		if (b.lifted_by && resolved[b.lifted_by]?.value === true) continue;
		const capped = !rule.order ? clone(b.cap) : idx(rule, out.value) <= idx(rule, b.cap) ? out.value : b.cap;
		const decides = !rule.order || same(b.cap, rule.order[0]);
		if (!same(capped, out.value) || (decides && out.source !== 'boundary')) out = entry(capped, 'boundary', b.reason, { fixed: true });
	}
	const p = rule.prerequisite;
	if (p && same(out.value, p.when) && p.requires === 'remote_pin' && !device.has_remote_pin) out = entry(p.otherwise, 'prerequisite', 'prerequisite_missing', { fixed: out.fixed });
	if (expired && rule.strictest !== null && rule.strictest !== undefined) out = entry(clone(rule.strictest), 'access_expired', 'access_expired', { fixed: true });
	return out;
}

/** Earlier rules the boundaries of a rule read: every lifted_by rule, and control_path for hub_only. */
function dependsOn(rule) {
	const deps = [];
	for (const b of rule.boundaries) {
		if (b.lifted_by && !deps.includes(b.lifted_by)) deps.push(b.lifted_by);
		if (b.boundary === 'hub_only' && !deps.includes('control_path')) deps.push('control_path');
	}
	return deps;
}

/**
 * A rule the boundaries read can change at its expires_at. Walking those ends in time order, before the
 * entry's own end: at the first end that changes the value, the entry carries that end and the value from then on.
 */
function dependencyEnds(rule, pre, eff, { access, profile, device, expired, resolved }) {
	const ends = dependsOn(rule)
		.filter((d) => resolved[d]?.expires_at)
		.map((d) => [Date.parse(resolved[d].expires_at), d])
		.sort((a, b) => a[0] - b[0]);
	const own = eff.expires_at ? Date.parse(eff.expires_at) : null;
	const after = { ...resolved };
	for (const [tEnd, dep] of ends) {
		if (own !== null && own <= tEnd) break;
		const d = resolved[dep];
		after[dep] = { ...d, value: d.then, expires_at: null, then: null };
		const then = clamp(rule, pre, { access, profile, device, expired, resolved: after });
		if (!same(then.value, eff.value)) return { ...eff, expires_at: d.expires_at, then: then.value };
	}
	return eff;
}

/** control_path on a TV or watch: hub becomes checked (reason platform_needs_key); an expired guest keeps hub. */
function platformNeedsKey(platform, e) {
	if (!KEY_PLATFORMS.includes(platform) || e.source === 'access_expired') return e;
	let out = e;
	if (e.value === 'hub') out = { ...e, value: 'checked', source: 'boundary', reason: 'platform_needs_key', locked: true, fixed: true, user_choice: false };
	if (out.then === 'hub') out = { ...out, then: 'checked' };
	// Nothing changes at the end.
	if (out.source === 'boundary' && out.expires_at && same(out.then, out.value)) out = { ...out, expires_at: null, then: null };
	return out;
}

/** The device gets its own Home Assistant key: checked or direct, or hub with remote access. */
export function holdsKey(eff) {
	const path = eff.control_path?.value;
	return path === 'checked' || path === 'direct' || eff.remote_access?.value === true;
}

export function resolve({ platform, access, profile, overrides, device, now = NOW() }) {
	const tNow = Date.parse(now);
	const expired = access.kind === 'guest' && access.expires_at && tNow >= Date.parse(access.expires_at);
	const byRule = Object.fromEntries((overrides || []).map((o) => [o.rule, o]));
	const fixedKeys = new Set(profile.fixed || []);
	const ucKeys = new Set(profile.user_choice || []);
	const out = {};
	for (const rule of CATALOG.rules) {
		const key = rule.key;
		if (!rule.platforms.includes(platform)) {
			out[key] = entry(null, 'not_applicable', 'not_applicable', { fixed: true });
			continue;
		}
		if (rule.requires_child_protection && !profile.child_protection) {
			out[key] = entry(null, 'not_applicable', 'requires_child_protection', { fixed: true });
			continue;
		}
		if (rule.profile_kinds && !rule.profile_kinds.includes(profile.kind || 'person')) {
			out[key] = entry(null, 'not_applicable', 'requires_profile_kind', { fixed: true });
			continue;
		}
		const fixed = !rule.fixed_checkbox || fixedKeys.has(key);
		const has = Object.prototype.hasOwnProperty.call(profile.rules || {}, key) && profile.rules[key] !== undefined;
		// Parental controls, a guest device or a guest kind without a stored control path: the hub path.
		const hubDefault = profile.child_protection || access.kind === 'guest' || ['guest', 'babysitter', 'house_sitter'].includes(profile.kind);
		const value = has ? profile.rules[key] : key === 'control_path' && hubDefault ? 'hub' : rule.default;
		const source = has ? 'profile' : 'default';
		const uc = rule.user_choice_allowed && ucKeys.has(key);
		const reason = uc ? 'user_choice' : source === 'profile' ? 'profile' : 'catalog_default';
		const base = entry(resolveOwnArea(key, value, device.own_area_id), source, reason, { uc, fixed });
		let eff = clamp(rule, base, { access, profile, device, expired, resolved: out });
		let pre = base;
		const o = byRule[key];
		const oEnd = o?.expires_at ? Date.parse(o.expires_at) : null;
		if (o && !fixed && (oEnd === null || tNow < oEnd)) {
			const oUc = rule.user_choice_allowed && !!o.user_choice;
			const oReason = oUc ? 'user_choice' : oEnd ? 'device_temporary' : 'device';
			const dev = entry(resolveOwnArea(key, o.value, device.own_area_id), 'device', oReason, { uc: oUc });
			const adj = clamp(rule, dev, { access, profile, device, expired, resolved: out });
			// A temporary adjustment, also one a boundary caps, ends with its value from then on.
			if (oEnd !== null && (adj.source === 'device' || !same(adj.value, eff.value))) {
				adj.expires_at = o.expires_at;
				adj.then = eff.value;
			}
			eff = adj;
			pre = dev;
		}
		eff = dependencyEnds(rule, pre, eff, { access, profile, device, expired, resolved: out });
		if (key === 'control_path') eff = platformNeedsKey(platform, eff);
		eff.profile_value = has || source === 'default' ? clone(value) : null;
		// A child's watch switches only through Home Assistant: kids_watch needs remote access.
		if (key === 'kids_watch' && out.remote_access.value !== true) {
			eff = { ...entry(null, 'not_applicable', 'requires_remote_access', { fixed: true }), profile_value: eff.profile_value };
		}
		out[key] = eff;
	}
	if (out.remote_control && out.remote_control.source !== 'not_applicable' && out.remote_access.value !== true) {
		out.remote_control = { ...entry(null, 'not_applicable', 'requires_remote_access', { fixed: true }), profile_value: out.remote_control.profile_value };
	}
	if (out.location_required && out.location_required.source !== 'not_applicable' && (out.location.value ?? 'off') === 'off') {
		out.location_required = { ...entry(null, 'not_applicable', 'requires_location', { fixed: true }), profile_value: out.location_required.profile_value };
	}
	const path = out.control_path.value;
	for (const rule of CATALOG.rules) {
		out[rule.key].enforcement = (path === 'hub' && rule.enforcement_with_hub_path) || ((path === 'hub' || path === 'checked') && rule.enforcement_with_checked_path) || rule.enforcement;
	}
	return out;
}

function loosens(rule, before, after) {
	if (!rule || !['critical', 'sensitive'].includes(rule.security)) return false;
	if (before === null || after === null || before === undefined || after === undefined) return false;
	if (Array.isArray(rule.order)) {
		const a = idx(rule, before);
		const b = idx(rule, after);
		return a >= 0 && b > a;
	}
	if (rule.key === 'areas') {
		if (after.mode === 'all') return before.mode !== 'all';
		if (before.mode === 'all') return false;
		return (after.area_ids || []).some((x) => !(before.area_ids || []).includes(x));
	}
	if (rule.key === 'remote_control') {
		const rank = { hidden: 0, view: 1, control: 2 };
		return rank[after.default] > rank[before.default];
	}
	if (rule.key === 'setup_devices') return !!after.enabled && (!before.enabled || (after.kinds || []).some((k) => !(before.kinds || []).includes(k)));
	return false;
}

function presetEnd(preset, now = new Date()) {
	const d = new Date(now);
	if (preset === '1h') return new Date(d.getTime() + 3600_000);
	if (preset === '7d') return new Date(d.getTime() + 7 * 86400_000);
	if (preset === 'today') {
		d.setHours(24, 0, 0, 0);
		return d;
	}
	if (preset === 'tomorrow_morning') {
		if (d.getHours() >= 5) d.setDate(d.getDate() + 1);
		d.setHours(7, 0, 0, 0);
		return d;
	}
	return null;
}

// ── fake hub ─────────────────────────────────────────────────────────────────

const STARTERS = { adult: 'p-adult', child: 'p-child', guest: 'p-guest', wall_panel: 'p-wall', apple_tv: 'p-tv' };

export const AREAS = [
	{ id: 'a-wohnen', name: 'Wohnzimmer', is_active: true, tags: ['eg'], entity_count: 6 },
	{ id: 'a-kueche', name: 'Küche', is_active: true, tags: ['eg'], entity_count: 4 },
	{ id: 'a-kid', name: 'Kinderzimmer', is_active: true, tags: [], entity_count: 3 },
	{ id: 'a-gast', name: 'Gästezimmer', is_active: true, tags: [], entity_count: 2 },
	{ id: 'a-buero', name: 'Büro', is_active: true, tags: [], entity_count: 3 },
	{ id: 'a-garten', name: 'Garten', is_active: true, tags: ['aussen'], entity_count: 2 },
];

export const ENTITIES = [
	{ entity_id: 'lock.front_door', friendly_name: 'Haustür', domain: 'lock' },
	{ entity_id: 'alarm_control_panel.security', friendly_name: 'Alarmanlage', domain: 'alarm_control_panel' },
	{ entity_id: 'binary_sensor.smoke_kitchen', friendly_name: 'Rauchmelder Küche', domain: 'binary_sensor', attributes: { device_class: 'smoke' } },
	{ entity_id: 'light.ceiling_lights', friendly_name: 'Deckenlicht', domain: 'light' },
	{ entity_id: 'light.nachtlicht', friendly_name: 'Nachtlicht', domain: 'light' },
	{ entity_id: 'cover.living_room_window', friendly_name: 'Rollladen', domain: 'cover' },
];

const ago = (min) => new Date(Date.now() - min * 60_000).toISOString();
const inMin = (min) => new Date(Date.now() + min * 60_000).toISOString();

function client(overrides) {
	return {
		is_active: true,
		is_connected: true,
		access: 'member',
		expires_at: null,
		set_up_by: null,
		has_ha_token: true,
		owner_label: null,
		own_area_id: null,
		has_remote_pin: false,
		ha_user_id: 'ha-user',
		ha_token_status: 'active',
		ha_token_pending_since: null,
		platform: 'ios',
		app_version: '2026.12.0',
		app_build: '7',
		device_model: 'iPhone18,1',
		protocol_version: 1,
		update_required: false,
		created_at: ago(60 * 24 * 10),
		last_seen: ago(2),
		...overrides,
	};
}

/** The household of the mockup: parents, Kid (8) with parental controls, a guest, wall panel, Apple TV. */
export function defaultClients() {
	return [
		client({ id: 'c-owner-a', name: 'iPhone Owner A', owner_label: 'Owner A', profile_id: 'p-adult' }),
		client({ id: 'c-owner-b', name: 'iPhone Owner B', owner_label: 'Owner B', profile_id: 'p-adult', last_seen: ago(90), is_connected: false }),
		client({ id: 'c-kid', name: 'Kids iPad', owner_label: 'Kid', platform: 'ipados', device_model: 'iPad13,18', profile_id: 'p-child', own_area_id: 'a-kid' }),
		client({ id: 'c-visitor', name: 'iPhone Visitor', owner_label: 'Visitor', access: 'guest', expires_at: inMin(60 * 50), profile_id: 'p-guest', own_area_id: 'a-gast', set_up_by: { client_id: 'c-owner-b', name: 'iPhone Owner B', method: 'invite' } }),
		client({ id: 'c-panel', name: 'iPad Flur', platform: 'ipados', device_model: 'iPad16,3', profile_id: 'p-wall', own_area_id: 'a-wohnen', has_remote_pin: true }),
		client({ id: 'c-tv', name: 'Living room TV', platform: 'tvos', device_model: 'AppleTV14,1', profile_id: 'p-tv' }),
	];
}

export function createProfilesHub({ clients = defaultClients(), bumpSuggestion = {}, suggestionRules = {}, overrides = {}, profileName = 'Owner A', extra = null, extendDelay = 0, previewDelay = 0 } = {}) {
	const suggestions = clone(SUGGESTIONS.suggestions).map((s) => ({ ...s, version: bumpSuggestion[s.id] || s.version }));
	const sugById = Object.fromEntries(suggestions.map((s) => [s.id, s]));
	const state = {
		profiles: [],
		clients: clone(clients),
		overrides: clone(overrides), // client id -> [override]
		settings: { default_profiles: { ios: 'p-adult', ipados: 'p-wall', tvos: 'p-tv', watchos: 'p-adult', android: 'p-adult', android_tablet: 'p-wall', android_tv: 'p-tv', wearos: 'p-adult', guest: 'p-guest' }, guest_default_hours: 24 },
		seq: 1,
		audit: [],
	};
	const requests = [];

	function newProfile(id, sid, name) {
		const s = sid ? SUGGESTIONS.suggestions.find((x) => x.id === sid) : null;
		const body = materialize(s);
		return {
			id,
			name: name || (s ? s.name.de : 'Neues Profil'),
			description: s ? s.description.de : null,
			icon: s?.icon || 'mdi:account-cog',
			color: s?.color || null,
			...body,
			origin: s ? { suggestion_id: s.id, version: s.version } : null,
			rev: 1,
			created_at: ago(60 * 24 * 30),
			updated_at: ago(60 * 24 * 2),
			updated_by: profileName,
			has_remote_pin: false,
			remote_pin_fixed: false,
		};
	}
	for (const [sid, pid] of Object.entries(STARTERS)) state.profiles.push(newProfile(pid, sid));
	if (extra) extra(state, { newProfile, materialize });

	const prof = (id) => state.profiles.find((p) => p.id === id);
	const cli = (id) => state.clients.find((c) => c.id === id);
	const ovs = (cid) => (state.overrides[cid] ||= []);

	/** Effective rules of a device at time t (ms; adjustments that ended by then do not count). */
	function effectiveAt(c, t, st = state) {
		const p = st.profiles.find((x) => x.id === c.profile_id);
		const pinOwn = !!c.has_remote_pin;
		const pinProfile = !!p.has_remote_pin;
		return resolve({
			platform: c.platform,
			access: { kind: c.access, expires_at: c.expires_at },
			profile: p,
			overrides: (st.overrides[c.id] || []).filter((o) => !o.expires_at || Date.parse(o.expires_at) > t),
			device: { own_area_id: c.own_area_id, has_remote_pin: (pinOwn && !p.remote_pin_fixed) || pinProfile },
			now: new Date(t).toISOString(),
		});
	}

	function effectiveOf(c, st = state) {
		return effectiveAt(c, Date.now(), st);
	}

	/** resolver.fmt_ts: ISO 8601 in UTC with "Z", seconds precision. */
	const fmtTs = (t) => new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z');
	/** catalog.DEVICE_PLATFORMS */
	const DEVICE_PLATFORMS = ['ios', 'ipados', 'tvos', 'watchos', 'android', 'android_tablet', 'android_tv', 'wearos'];
	/** store.pairing_hub_remote: a device paired now with this profile (no adjustments) per access and platform. */
	function pairingHubRemote(p) {
		const out = {};
		for (const access of ['member', 'guest']) {
			out[access] = {};
			for (const platform of DEVICE_PLATFORMS) {
				const eff = resolve({ platform, access: { kind: access, expires_at: access === 'guest' ? '9999-12-31T00:00:00Z' : null }, profile: p, overrides: [], device: { own_area_id: null, has_remote_pin: false } });
				out[access][platform] = isHubRemote(eff);
			}
		}
		return out;
	}

	const isHubRemote = (eff) => eff.control_path?.value === 'hub' && eff.remote_access?.value === true;

	/**
	 * Like the hub: hub_remote_now (on "Hub only" with remote access now) and hub_remote_at (the
	 * earliest later scheduled end of any rule or adjustment after which it is), evaluated at each end.
	 */
	function hubRemoteFields(c) {
		const t0 = Date.now();
		const eff = effectiveAt(c, t0);
		if (isHubRemote(eff)) return { hub_remote_now: true, hub_remote_at: null };
		// Like store.hub_remote_schedule: the ends of the device's adjustments and of its guest access,
		// each evaluated one second after it; the answer is the end itself, UTC with "Z" and no milliseconds.
		const ends = new Set();
		for (const o of state.overrides[c.id] || []) if (o.expires_at && Date.parse(o.expires_at) > t0) ends.add(Date.parse(o.expires_at));
		if (c.access === 'guest' && c.expires_at && Date.parse(c.expires_at) > t0) ends.add(Date.parse(c.expires_at));
		for (const t of [...ends].sort((a, b) => a - b)) {
			if (isHubRemote(effectiveAt(c, t + 1000))) return { hub_remote_now: false, hub_remote_at: fmtTs(t) };
		}
		return { hub_remote_now: false, hub_remote_at: null };
	}

	function profileOut(p) {
		const devs = state.clients.filter((c) => c.profile_id === p.id && !c.pending_deletion);
		const s = p.origin ? sugById[p.origin.suggestion_id] : null;
		// Like the hub: a profile without a stored level reports the resolver's fallback.
		const rules = clone(p.rules || {});
		const filled = rules.control_path === undefined;
		if (filled) rules.control_path = p.child_protection || ['guest', 'babysitter', 'house_sitter'].includes(p.kind) ? 'hub' : CATALOG.rules.find((r) => r.key === 'control_path').default;
		return {
			...clone(p),
			rules,
			hub_remote_pairing: pairingHubRemote(p),
			suggestion_update: s && s.version > p.origin.version ? { version: s.version } : null,
			catalog_defaults: filled ? ['control_path'] : [],
			default_for: Object.entries(state.settings.default_profiles).filter(([, v]) => v === p.id).map(([k]) => k),
			device_count: devs.length,
			adjusted_device_count: devs.filter((c) => (state.overrides[c.id] || []).length).length,
			guest_device_count: devs.filter((c) => c.access === 'guest').length,
		};
	}

	/** Access now: switch for access hours or kind guest/house sitter/babysitter; c.access_open mocks the schedule. */
	function accessNow(c) {
		const p = prof(c.profile_id);
		const eff = effectiveOf(c);
		const schedule = !!eff.access_hours?.value?.enabled;
		const hasSwitch = schedule || ['guest', 'house_sitter', 'babysitter'].includes(p.kind || 'person');
		const sw = hasSwitch ? c.access_switch !== false : null;
		// C.access_early mocks a running early access.
		const early = schedule && c.access_early ? c.access_early : null;
		const schedOk = !schedule || c.access_open !== false || !!early;
		const open = schedOk && sw !== false;
		const out = {
			open,
			reason: open ? null : sw === false ? 'switch' : 'schedule',
			until: open && schedule ? (early ? early.until : c.access_until || inMin(4 * 60)) : null,
			opens_at: !open && sw !== false ? c.access_opens_at || inMin(20 * 60) : null,
			schedule,
			switch: sw,
		};
		if (early) out.early = early;
		return out;
	}

	function clientOut(c) {
		const p = prof(c.profile_id);
		const eff = effectiveOf(c);
		const list = state.overrides[c.id] || [];
		return {
			...clone(c),
			profile_name: p.name,
			profile_kind: p.kind || 'person',
			child_protection: !!p.child_protection,
			override_count: list.length,
			temporary_override_count: list.filter((o) => o.expires_at).length,
			remote_allowed: c.unresolved ? null : eff.remote_access.value === true,
			location_rule: eff.location.source === 'not_applicable' ? null : eff.location.value,
			location_required: eff.location_required.value === true,
			location_shared: c.location_shared ?? null,
			location_at: c.location_at ?? null,
			// c.stale_key mocks a key the device lost but the hub has not rotated yet.
			has_ha_token: c.ha_token_status === 'active' && (holdsKey(eff) || !!c.stale_key),
			// c.unresolved mocks a device whose rules the hub cannot resolve (control_path null).
			control_path: c.unresolved ? null : eff.control_path.value,
			access_now: accessNow(c),
		};
	}

	function effectiveResponse(c) {
		const p = prof(c.profile_id);
		const eff = effectiveOf(c);
		return {
			client_id: c.id,
			platform: c.platform,
			access: { kind: c.access, expires_at: c.expires_at, set_up_by: c.set_up_by, hours: accessNow(c), time_zone: 'Europe/Zurich' },
			profile: { id: p.id, name: p.name, kind: p.kind || 'person', rev: p.rev, child_protection: !!p.child_protection },
			own_area_id: c.own_area_id,
			has_remote_pin: !!c.has_remote_pin,
			remote_pin_source: c.has_remote_pin && !p.remote_pin_fixed ? 'device' : p.has_remote_pin ? 'profile' : null,
			rules: eff,
			overrides: (state.overrides[c.id] || []).map((o) => ({ origin: 'admin', created_by_client_id: null, ...clone(o) })),
			...hubRemoteFields(c),
		};
	}

	/** Preview of a mutation (dry run): effective values before/after on every device. */
	/** The hub's preview entry fields of a rule (services/profiles/changes.py _public). */
	const pub = (e) => ({ value: e.value, source: e.source, reason: e.reason, locked: e.locked, fixed: e.fixed, user_choice: e.user_choice, expires_at: e.expires_at ?? null, then: e.then ?? null });
	const resolvable = (c) => !c.unresolved;

	/**
	 * Preview of a mutation (dry run), shaped like the hub's (changes.py): devices the change touches
	 * first (affected_count counts them), then devices it leaves as they are that are or become "Hub only"
	 * with remote access (changes [], ha_key and remote_access "unchanged"). A device whose rules cannot
	 * be resolved has no entry. Watches removed follow _follow_phones.
	 */
	function preview(mutate) {
		const before = Object.fromEntries(state.clients.filter(resolvable).map((c) => [c.id, effectiveOf(c)]));
		const beforeOv = clone(state.overrides);
		const beforeKey = Object.fromEntries(Object.entries(before).map(([id, e]) => [id, holdsKey(e)]));
		const beforeProfile = Object.fromEntries(state.clients.map((c) => [c.id, c.profile_id]));
		const beforeChild = Object.fromEntries(state.clients.map((c) => [c.id, !!prof(c.profile_id)?.child_protection]));
		// Like the hub: the pair (hub_remote_now, hub_remote_at) before the change, at the same moment.
		const beforePair = Object.fromEntries(state.clients.filter(resolvable).map((c) => [c.id, hubRemoteFields(c)]));
		const snapshot = clone({ profiles: state.profiles, clients: state.clients, overrides: state.overrides, settings: state.settings });
		const err = mutate();
		const devices = [];
		const listed = [];
		const loosened = new Set();
		const watchesRemoved = [];
		if (!err) {
			const afterOf = Object.fromEntries(state.clients.filter(resolvable).map((c) => [c.id, effectiveOf(c)]));
			for (const c of state.clients) {
				// Like the hub: only devices that existed before and still resolve after.
				if (!before[c.id] || !afterOf[c.id]) continue;
				const after = afterOf[c.id];
				const changes = [];
				for (const rule of CATALOG.rules) {
					const b = pub(before[c.id][rule.key]);
					const a = pub(after[rule.key]);
					if (same(b, a)) continue;
					const l = loosens(rule, b.value, a.value);
					if (l) loosened.add(rule.key);
					changes.push({ rule: rule.key, before: b, after: a, loosens: l });
				}
				const removed = (beforeOv[c.id] || []).filter((o) => !(state.overrides[c.id] || []).some((x) => x.rule === o.rule)).map((o) => o.rule).sort();
				const keyAfter = holdsKey(after);
				const ha_key = beforeKey[c.id] === keyAfter ? 'unchanged' : keyAfter ? 'restored' : 'removed';
				const remB = before[c.id].remote_access.value === true;
				const remA = after.remote_access.value === true;
				const remote_access = remB === remA ? 'unchanged' : remA ? 'enabled' : 'disabled';
				const hr = hubRemoteFields(c);
				const moved = beforeProfile[c.id] !== c.profile_id;
				if (changes.length || removed.length || ha_key !== 'unchanged' || remote_access !== 'unchanged' || moved) {
					devices.push({ client_id: c.id, name: c.name, changes, overrides_removed: removed, ha_key, remote_access, ...hr });
				} else if (hr.hub_remote_now !== beforePair[c.id]?.hub_remote_now || hr.hub_remote_at !== beforePair[c.id]?.hub_remote_at) {
					// The change alters when the device is on "Hub only" with remote access without changing its rules now.
					listed.push({ client_id: c.id, name: c.name, changes: [], overrides_removed: [], ha_key: 'unchanged', remote_access: 'unchanged', ...hr });
				}
			}
			// _follow_phones: a watch of an adult phone goes when the phone's watch rule is off through
			// "Hub only"; a child's watch goes when the phone's watch rule is off.
			for (const w of state.clients.filter((x) => ['watchos', 'wearos'].includes(x.platform) && x.is_active !== false)) {
				const phone = afterOf[w.set_up_by?.client_id];
				if (!phone) continue;
				const child = !!prof(state.clients.find((x) => x.id === w.set_up_by.client_id)?.profile_id)?.child_protection;
				if (!child) {
					if (phone.watch?.value !== true && phone.watch?.reason === 'boundary_hub_only') watchesRemoved.push({ client_id: w.id, name: w.name, reason: 'hub_only' });
					continue;
				}
				if (phone.watch?.value !== true) {
					const reason = phone.kids_watch?.reason === 'requires_remote_access' ? 'remote_access_off' : !beforeChild[w.set_up_by.client_id] ? 'parental_controls' : 'kids_watch_off';
					watchesRemoved.push({ client_id: w.id, name: w.name, reason });
				}
			}
		}
		Object.assign(state, snapshot);
		const out = { dry_run: true, affected_count: devices.length, loosened_rules: [...loosened].sort(), devices: [...devices, ...listed] };
		if (watchesRemoved.length) out.watches_removed = watchesRemoved;
		return err || out;
	}

	function dropFixedOverrides(profileId) {
		const p = prof(profileId);
		for (const c of state.clients.filter((x) => x.profile_id === profileId)) {
			state.overrides[c.id] = (state.overrides[c.id] || []).filter((o) => !p.fixed.includes(o.rule));
		}
	}

	const E = (status, code) => ({ __error: true, status, code });

	function audit(action, fields) {
		state.audit.unshift({ id: state.audit.length + 1, ts: NOW(), action, actor: profileName, profile_id: null, client_id: null, rule: null, before: null, after: null, note: null, ...fields });
	}

	function patchProfile(p, body) {
		if (body.name !== undefined) {
			if (state.profiles.some((x) => x.id !== p.id && x.name.toLowerCase() === body.name.toLowerCase())) return E(422, 'name.duplicate');
			p.name = body.name;
		}
		if (body.description !== undefined) p.description = body.description;
		if (body.kind !== undefined) p.kind = body.kind;
		if (body.child_protection !== undefined) {
			if (body.child_protection && state.clients.some((c) => c.profile_id === p.id && c.access === 'guest')) return E(422, 'profile.guest_child_protection');
			p.child_protection = body.child_protection;
		}
		if (body.remote_pin !== undefined) {
			p.has_remote_pin = body.remote_pin !== null;
			if (!p.has_remote_pin) p.remote_pin_fixed = false;
		}
		if (body.remote_pin_fixed !== undefined) p.remote_pin_fixed = !!body.remote_pin_fixed && p.has_remote_pin;
		if (body.rules) p.rules = { ...p.rules, ...clone(body.rules) };
		if (body.fixed) p.fixed = [...new Set([...body.fixed, ...CATALOG.rules.filter((r) => !r.fixed_checkbox).map((r) => r.key)])].sort();
		if (body.user_choice) p.user_choice = [...body.user_choice].sort();
		dropFixedOverrides(p.id);
		return null;
	}

	function assign(c, profileId, discard) {
		const p = prof(profileId);
		if (!p) return E(404, 'not_found');
		if (c.access === 'guest' && p.child_protection) return E(422, 'profile.guest_child_protection');
		c.profile_id = profileId;
		state.overrides[c.id] = discard ? [] : (state.overrides[c.id] || []).filter((o) => !p.fixed.includes(o.rule));
		return null;
	}

	function setOverride(c, key, body) {
		const p = prof(c.profile_id);
		const rule = CATALOG.rules.find((r) => r.key === key);
		if (!rule) return E(422, 'override.value');
		const eff = effectiveOf(c)[key];
		if (eff.source === 'not_applicable') return E(422, 'override.not_applicable');
		if (!rule.fixed_checkbox || p.fixed.includes(key)) return E(422, 'override.fixed');
		if (eff.source === 'boundary') return E(422, 'override.boundary');
		if (body.expires && body.expires_at) return E(422, 'override.expires_at');
		const expires_at = body.expires ? presetEnd(body.expires).toISOString() : body.expires_at || null;
		const list = ovs(c.id).filter((o) => o.rule !== key);
		const profileValue = Object.prototype.hasOwnProperty.call(p.rules, key) ? p.rules[key] : rule.default;
		if (!(same(body.value, profileValue) && !body.user_choice)) {
			list.push({ rule: key, value: clone(body.value), user_choice: !!body.user_choice, expires_at, note: body.note ?? null, created_by: profileName, created_at: NOW() });
		}
		state.overrides[c.id] = list;
		return null;
	}

	function bulk(body) {
		const results = [];
		const run = () => {
			for (const id of body.client_ids) {
				const c = cli(id);
				if (!c) {
					results.push({ client_id: id, status: 'skipped', reason: 'not_found' });
					continue;
				}
				const before = JSON.stringify([c, state.overrides[c.id] || []]);
				let err = null;
				if (body.action === 'assign_profile') err = assign(c, body.profile_id, body.discard_overrides);
				else if (body.action === 'set_own_area') c.own_area_id = body.area_id;
				else if (body.action === 'set_override') err = setOverride(c, body.rule, body);
				else if (body.action === 'remove_override') state.overrides[c.id] = (state.overrides[c.id] || []).filter((o) => o.rule !== body.rule);
				if (err) {
					const reason = { 'profile.guest_child_protection': 'guest_child_protection', 'override.fixed': 'rule_fixed', 'override.boundary': 'boundary', 'override.not_applicable': 'not_applicable' }[err.code] || 'invalid_value';
					results.push({ client_id: id, status: 'skipped', reason });
				} else results.push({ client_id: id, status: JSON.stringify([c, state.overrides[c.id] || []]) === before ? 'unchanged' : 'changed', reason: null });
			}
		};
		if (body.dry_run) {
			const pv = preview(() => (run(), null));
			return { dry_run: true, results, preview: pv };
		}
		run();
		return { dry_run: false, results, preview: null };
	}

	/** body: what the tests compare (PINs masked); raw: what was sent (for the schema check). */
	function log(method, path, body, raw = body) {
		requests.push([method, path, body]);
		if (process.env.E2E_REQUEST_LOG) fs.appendFileSync(process.env.E2E_REQUEST_LOG, JSON.stringify({ method, path, body: raw }) + '\n');
	}

	async function handler({ path, method, request, url, json }) {
		const body = ['POST', 'PUT', 'PATCH'].includes(method) ? request.postDataJSON?.() ?? null : null;
		const dry = url.searchParams.get('dry_run') === 'true';
		// previewDelay(path, body) → ms: a slow dry-run answer (also bulk dry runs), for the stale-preview tests.
		const delay = (dry || body?.dry_run === true) && previewDelay ? (typeof previewDelay === 'function' ? previewDelay(path + url.search, body) : previewDelay) : 0;
		if (delay) await new Promise((r) => setTimeout(r, delay));
		const send = (res, ok = 200) => (res?.__error ? json({ detail: { code: res.code, message: res.code } }, res.status) : json(res, dry ? 202 : ok));
		let m;

		if (path === '/profile-rules') return json(CATALOG);
		if (path === '/areas' && method === 'GET') return json(AREAS);
		if (path === '/tags') return json({ tags: ['eg', 'aussen'] });
		if (path === '/ha/entities') return json(ENTITIES);
		if (path === '/settings/profiles') {
			if (method === 'PUT') {
				log(method, path, body);
				const g = prof(body.default_profiles.guest);
				if (g?.child_protection) return json({ detail: { code: 'profile.guest_child_protection' } }, 422);
				state.settings = clone(body);
			}
			return json(state.settings);
		}
		if (path === '/profiles' && method === 'GET') return json({ profiles: state.profiles.map(profileOut).sort((a, b) => a.name.localeCompare(b.name)), max_profiles: CATALOG.limits.max_profiles });
		if (path === '/profiles' && method === 'POST') {
			log(method, path, body);
			const p = newProfile(`p-new-${state.seq++}`, null, body.name);
			p.child_protection = !!body.child_protection;
			p.kind = body.kind || 'person';
			if (body.rules) p.rules = { ...p.rules, ...clone(body.rules) };
			p.updated_at = NOW();
			state.profiles.push(p);
			return json(profileOut(p), 201);
		}
		if (path === '/profiles/from-device' && method === 'POST') {
			log(method, path, body);
			const c = cli(body.client_id);
			const src = prof(c.profile_id);
			const p = { ...clone(src), id: `p-new-${state.seq++}`, name: body.name, origin: null, rev: 1, updated_at: NOW() };
			for (const o of state.overrides[c.id] || []) p.rules[o.rule] = clone(o.value);
			state.profiles.push(p);
			if (body.assign !== false) {
				c.profile_id = p.id;
				state.overrides[c.id] = [];
			}
			return json(profileOut(p), 201);
		}
		if ((m = path.match(/^\/profiles\/([^/]+)$/))) {
			const p = prof(m[1]);
			if (!p) return json({ detail: 'not found' }, 404);
			if (method === 'GET') return json(profileOut(p));
			if (method === 'PATCH') {
				log(method, path + (dry ? '?dry_run=true' : ''), body && 'remote_pin' in body ? { ...body, remote_pin: body.remote_pin ? '********' : null } : body, body);
				if (dry) return send(preview(() => patchProfile(p, body)));
				const err = patchProfile(p, body);
				if (err) return send(err);
				p.rev += 1;
				p.updated_at = NOW();
				p.updated_by = profileName;
				return json(profileOut(p));
			}
			if (method === 'DELETE') {
				const replacement = url.searchParams.get('replacement');
				log(method, `${path}?${url.searchParams}`, null);
				if (Object.values(state.settings.default_profiles).includes(p.id)) return json({ detail: { code: 'profile_is_default' } }, 409);
				const users = state.clients.filter((c) => c.profile_id === p.id);
				if (users.length && !replacement) return json({ detail: { code: 'profile_in_use' } }, 409);
				const run = () => {
					for (const c of users) {
						const e = assign(c, replacement, false);
						if (e) return e;
					}
					state.profiles = state.profiles.filter((x) => x.id !== p.id);
					return null;
				};
				if (dry) return send(preview(run));
				const err = run();
				return err ? send(err) : route204(json);
			}
		}
		if ((m = path.match(/^\/profiles\/([^/]+)\/duplicate$/))) {
			log(method, path, body);
			const src = prof(m[1]);
			const p = { ...clone(src), id: `p-new-${state.seq++}`, name: body?.name || `Kopie von ${src.name}`, origin: null, rev: 1, updated_at: NOW() };
			state.profiles.push(p);
			return json(profileOut(p), 201);
		}
		if ((m = path.match(/^\/profiles\/([^/]+)\/apply-suggestion$/))) {
			log(method, path + (dry ? '?dry_run=true' : ''), body);
			const p = prof(m[1]);
			const s = sugById[p.origin?.suggestion_id];
			if (!s || s.version <= p.origin.version) return json({ detail: { code: 'no_suggestion_update' } }, 409);
			const run = () => {
				const fresh = materialize(SUGGESTIONS.suggestions.find((x) => x.id === s.id));
				// The bumped test version brings a stricter bedtime.
				fresh.rules.kids_quiet_hours = [{ days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], start: '19:30', end: '06:30' }];
				// suggestionRules mocks further changes of the new version.
				Object.assign(fresh.rules, clone(suggestionRules[s.id] || {}));
				Object.assign(p, { rules: fresh.rules, fixed: fresh.fixed, user_choice: fresh.user_choice, child_protection: fresh.child_protection });
				p.origin = { suggestion_id: s.id, version: s.version };
				return null;
			};
			if (dry) return send(preview(run));
			run();
			p.rev += 1;
			return json(profileOut(p));
		}
		if (path === '/profile-suggestions') {
			return json({
				catalog_version: SUGGESTIONS.catalog_version,
				suggestions: suggestions.map((s) => ({
					id: s.id,
					version: s.version,
					group: s.group,
					starter: s.starter,
					name: s.name,
					description: s.description,
					icon: s.icon,
					color: s.color,
					child_protection: s.child_protection,
					intended_for: s.intended_for,
					profile_ids: state.profiles.filter((p) => p.origin?.suggestion_id === s.id).map((p) => p.id),
					rules: s.rules,
				})),
			});
		}
		if ((m = path.match(/^\/profile-suggestions\/([^/]+)$/)) && method === 'POST') {
			log(method, path, body);
			const s = SUGGESTIONS.suggestions.find((x) => x.id === m[1]);
			let name = body?.name || s.name.de;
			for (let n = 2; state.profiles.some((p) => p.name === name); n++) name = `${s.name.de} (${n})`;
			const p = newProfile(`p-new-${state.seq++}`, s.id, name);
			p.updated_at = NOW();
			state.profiles.push(p);
			return json(profileOut(p), 201);
		}

		// ── devices ──
		if (path === '/pairing/clients' && method === 'GET') return json(state.clients.map(clientOut));
		if (path === '/pairing/clients/bulk' && method === 'POST') {
			log(method, path, body);
			return json(bulk(body));
		}
		if ((m = path.match(/^\/pairing\/clients\/([^/]+)$/))) {
			const c = cli(m[1]);
			if (!c) return json({ detail: 'not found' }, 404);
			if (method === 'GET') return json(clientOut(c));
			if (method === 'PATCH') {
				log(method, path, body);
				Object.assign(c, body);
				return json(clientOut(c));
			}
		}
		if ((m = path.match(/^\/pairing\/clients\/([^/]+)\/access-switch$/)) && method === 'PUT') {
			const c = cli(m[1]);
			if (!c) return json({ detail: 'not found' }, 404);
			log(method, path, body);
			if (accessNow(c).switch === null) return json({ detail: 'no_access_switch' }, 409);
			c.access_switch = !!body.on;
			return json(clientOut(c));
		}
		if ((m = path.match(/^\/pairing\/clients\/([^/]+)\/(effective|profile|own-area|remote-pin|overrides\/([^/]+)|activity|kids-status|kids-override)$/))) {
			const c = cli(m[1]);
			if (!c) return json({ detail: 'not found' }, 404);
			const what = m[2];
			// store.effective_view: a device whose rules cannot be resolved answers 404.
			if (what === 'effective') return c.unresolved ? json({ detail: { code: 'client_not_found', message: 'Client not found' } }, 404) : json(effectiveResponse(c));
			if (what === 'activity') return json({ client_id: c.id, max_events: 100, events: [{ id: 2, client_id: c.id, ts: ago(30), event: 'connected', detail: {} }] });
			if (what === 'kids-status') return json({ client_id: c.id, profile_id: c.profile_id, override_until: null, cooldowns: [] });
			if (what === 'kids-override') log(method, path, body);
			if (what === 'kids-override') return json({ client_id: c.id, profile_id: c.profile_id, override_until: method === 'POST' ? (body.until === 'bedtime_end' ? inMin(14 * 60) : inMin(body.minutes)) : null, cooldowns: [] });
			if (what === 'profile' && method === 'PUT') {
				log(method, path + (dry ? '?dry_run=true' : ''), body);
				if (dry) return send(preview(() => assign(c, body.profile_id, body.discard_overrides)));
				const from = c.profile_id;
				const err = assign(c, body.profile_id, body.discard_overrides);
				if (!err) audit('profile_assigned', { client_id: c.id, profile_id: body.profile_id, before: { profile_id: from }, after: { profile_id: body.profile_id, discard_overrides: !!body.discard_overrides } });
				return err ? send(err) : json(clientOut(c));
			}
			if (what === 'own-area' && method === 'PUT') {
				log(method, path, body);
				if (dry) return send(preview(() => ((c.own_area_id = body.area_id), null)));
				audit('own_area_set', { client_id: c.id, before: { own_area_id: c.own_area_id }, after: { own_area_id: body.area_id } });
				c.own_area_id = body.area_id;
				return json(effectiveResponse(c));
			}
			if (what === 'remote-pin' && method === 'PUT') {
				log(method, path, { pin: body.pin ? '********' : null }, body);
				if (body.pin && prof(c.profile_id).remote_pin_fixed) return json({ detail: { code: 'remote_pin.fixed' } }, 422);
				c.has_remote_pin = body.pin !== null;
				return json(effectiveResponse(c));
			}
			if (m[3]) {
				const key = m[3];
				if (method === 'PUT') {
					log(method, path + (dry ? '?dry_run=true' : ''), body);
					if (dry) return send(preview(() => setOverride(c, key, body)));
					const before = (state.overrides[c.id] || []).find((o) => o.rule === key);
					const err = setOverride(c, key, body);
					if (!err) audit('override_set', { client_id: c.id, rule: key, before: before ? { value: before.value } : { value: effectiveOf(c)[key].profile_value }, after: { value: body.value, expires_at: (state.overrides[c.id] || []).find((o) => o.rule === key)?.expires_at ?? null }, note: body.note ?? null });
					return err ? send(err) : json(effectiveResponse(c));
				}
				if (method === 'DELETE') {
					log(method, path + (dry ? '?dry_run=true' : ''), null);
					if (dry) return send(preview(() => ((state.overrides[c.id] = (state.overrides[c.id] || []).filter((o) => o.rule !== key)), null)));
					const before = (state.overrides[c.id] || []).find((o) => o.rule === key);
					if (before) audit('override_removed', { client_id: c.id, rule: key, before: { value: before.value } });
					state.overrides[c.id] = (state.overrides[c.id] || []).filter((o) => o.rule !== key);
					return json(effectiveResponse(c));
				}
			}
		}
		// api/pairing/guests.py extend_guest: hours from now or an explicit end; dry_run answers the
		// new end and when the guest is then on "Hub only" with remote access.
		if ((m = path.match(/^\/clients\/([^/]+)\/extend$/)) && method === 'PATCH') {
			const c = cli(m[1]);
			if (!c) return json({ detail: 'Client not found' }, 404);
			if (c.access !== 'guest') return json({ detail: 'Only guest clients can be extended' }, 400);
			log(method, path, body);
			if (extendDelay) await new Promise((r) => setTimeout(r, extendDelay));
			const end = body.expires_at ? fmtTs(Date.parse(body.expires_at)) : fmtTs(Date.now() + body.hours * 3600_000);
			if (body.dry_run) {
				const old = c.expires_at;
				c.expires_at = end;
				const hr = hubRemoteFields(c);
				c.expires_at = old;
				return json({ dry_run: true, expires_at: end, ...hr });
			}
			c.expires_at = end;
			// Like the hub: the real extension answers the same two fields.
			return json({ client_id: c.id, expires_at: end, ...hubRemoteFields(c) });
		}
		if (path === '/pairing/generate' && method === 'POST') {
			log(method, path, body);
			return json({ pin: '48213907', expires_in: 300, deep_link: 'halumar://pair?host=hub.local&port=8099&pin=48213907&tls_port=8100&fp=e39c8e8c00112233445566778899aabbccddeeff00112233445566778899031c', url: 'http://hub.local:8099' });
		}
		if (path === '/pairing/status') return json({ pairing_active: false });
		if (path === '/profile-audit') {
			const cid = url.searchParams.get('client_id');
			const pid = url.searchParams.get('profile_id');
			return json({ entries: state.audit.filter((e) => (!cid || e.client_id === cid) && (!pid || e.profile_id === pid)) });
		}
		return undefined;
	}

	return { state, requests, handler, effectiveOf, clientOut, profileOut, prof, cli };
}

function route204(json) {
	return json(null, 204);
}
