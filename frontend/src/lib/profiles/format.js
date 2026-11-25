/**
 * Values of profile rules: choices for the editor, readable text for
 * lists and previews, comparison and "does this loosen a security rule?".
 * The catalog (labels, order, defaults) is in $lib/profiles/catalog.svelte.js.
 */
import { t } from '$lib/i18n/index.svelte.js';
import { formatWeekdayTime } from '$lib/utils.js';
import { daysLabel, summarize, typeLabel } from '$lib/data/kids.js';
import { L, catalog } from './catalog.svelte.js';

/** Choices of the enum rules, in display order. Unknown enums fall back to the catalog order. */
export const ENUM_CHOICES = {
	app_settings: ['open', 'pin', 'hidden'],
	control_path: ['hub', 'checked', 'direct'],
	control_mode_max: ['basic', 'advanced', 'full'],
	quick_access: ['off', 'status', 'control'],
	card_style: ['calm', 'color'],
	appearance: ['system', 'light', 'dark'],
	notification_level: ['all', 'important', 'none'],
	notification_stack: ['off', 'banner', 'carousel'],
	location: ['off', 'foreground', 'background'],
	panel_off_mode: ['black', 'off'],
};

/** "When away" levels, strict → open. */
export const REMOTE_LEVELS = ['control', 'view', 'hidden'];
/**
 * Device types whose important alerts no profile hides (catalog
 * safety_entities): alarm, smoke, carbon monoxide, water, gas. "When away"
 * at most "show only". Fallback until the catalog is loaded.
 */
const SAFETY_FALLBACK = ['alarm_control_panel', 'binary_sensor:smoke', 'binary_sensor:carbon_monoxide', 'binary_sensor:moisture', 'binary_sensor:gas'];
export function safetyTypes() {
	const s = catalog.data?.safety_entities;
	if (!s) return SAFETY_FALLBACK;
	return [...(s.domains || []), ...(s.binary_sensor_device_classes || []).map((c) => `binary_sensor:${c}`)];
}
/** Device types offered in "When away" (plus whatever the value already contains). */
export const REMOTE_TYPES = ['lock', 'alarm_control_panel', 'cover:garage', 'cover:gate', 'cover', 'camera', 'climate', 'light', 'switch', 'media_player', 'vacuum', 'binary_sensor:smoke', 'binary_sensor:carbon_monoxide', 'binary_sensor:moisture', 'binary_sensor:gas'];

/** Minutes offered for the wall panel timers (0 = never). */
export const PANEL_MINUTES = [0, 1, 2, 5, 10, 15, 30, 60];
export const NIGHT_BRIGHTNESS = [5, 10, 20, 30, 50, 100];
/** Wall panel as a Home Assistant device. */
export const PANEL_SENSORS = ['battery', 'charging', 'screen', 'brightness', 'illuminance', 'battery_temperature'];
/** Volume steps of panel_announce. */
export const ANNOUNCE_VOLUMES = [20, 40, 50, 60, 70, 80, 90, 100];
export const PANEL_WAKE_DOMAINS = ['binary_sensor', 'person', 'device_tracker', 'input_boolean', 'event'];
export const PANEL_BRIGHTNESS_MODES = ['system', 'fixed', 'lux', 'schedule'];
export const PANEL_LEVELS = [5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100];
export const POWER_DOMAINS = ['light', 'switch', 'fan', 'input_boolean', 'cover', 'media_player', 'climate', 'vacuum', 'humidifier', 'automation'];
/** Kinds of the kids rule kids_requests ("Can I have more time?"). */
export const REQUEST_KINDS = ['stay_up', 'unlock_device', 'end_pause'];
/** Styles of the home screen head (rule home_header), in display order, and its parts. */
export const HOME_STYLES = ['weather_mood', 'compact'];
export const HOME_PARTS = ['status_chips', 'greeting', 'weather'];
export const HOME_HEADER_DEFAULT = { style: 'weather_mood', status_chips: true, greeting: true, weather: true };

export function clone(v) {
	return v === undefined ? undefined : JSON.parse(JSON.stringify(v));
}

/** JSON equality that keeps true and 1 apart and ignores key order. */
export function same(a, b) {
	if (a === b) return true;
	if (typeof a !== typeof b || a === null || b === null) return false;
	if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((x, i) => same(x, b[i]));
	if (typeof a === 'object') {
		if (Array.isArray(b)) return false;
		const ka = Object.keys(a);
		const kb = Object.keys(b);
		return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && same(a[k], b[k]));
	}
	return false;
}

export function enumChoices(rule) {
	return ENUM_CHOICES[rule?.key] || (Array.isArray(rule?.order) ? [...rule.order].reverse() : []);
}

export function enumLabel(key, value) {
	const k = `profiles.enum.${key}.${value}`;
	const s = t(k);
	return s === k ? String(value) : s;
}

export function levelLabel(level) {
	return t(`profiles.remote.${level}`);
}

function minutesLabel(n) {
	return n === 0 ? t('profiles.v.never') : t('profiles.v.minutes', { count: n });
}

/**
 * Readable value. ctx: { areaName(id) → string, ownAreaName? (device page) }.
 * Not applicable (null) → "–".
 */
export function formatValue(key, value, ctx = {}) {
	if (value === null || value === undefined) {
		if (key === 'kids_volume_max' || key === 'kids_rate_limit') return summarize(key === 'kids_volume_max' ? 'volume_max' : 'rate_limit', null);
		return '–';
	}
	const areaName = ctx.areaName || ((id) => id);
	switch (key) {
		case 'areas': {
			if (value.mode === 'all') return t('profiles.v.allAreas');
			const parts = [];
			if (value.own_area) parts.push(ctx.ownAreaName ? ctx.ownAreaName : t('profiles.v.ownArea'));
			for (const id of value.area_ids || []) parts.push(areaName(id));
			for (const tag of value.tags || []) parts.push(`#${tag}`);
			return parts.length ? shortList(parts) : t('profiles.v.noAreas');
		}
		case 'panel_start':
			if (value.mode === 'own_area') return t('profiles.v.ownArea');
			if (value.mode === 'area') return value.area_id ? areaName(value.area_id) : t('profiles.v.overview');
			return t('profiles.v.overview');
		case 'panel_night':
			if (value.mode === 'off') return t('profiles.v.off');
			if (value.mode === 'sun') return t('profiles.v.nightSun', { n: value.brightness });
			return t('profiles.v.nightSchedule', { start: value.start, end: value.end, n: value.brightness });
		case 'power_button':
			return value.enabled ? t('profiles.v.powerOn', { domains: (value.domains || []).map((d) => t(`profiles.domains.${d}`)).join(', ') }) : t('profiles.v.off');
		case 'kids_blocked_types':
			return summarize('blocked_types', value);
		case 'kids_quiet_hours':
			return summarize('quiet_hours', value);
		case 'kids_rate_limit':
			return summarize('rate_limit', value);
		case 'kids_rate_limit_all':
			return summarize('rate_limit_all', value);
		case 'kids_volume_max':
			return summarize('volume_max', value);
		case 'kids_color_lock':
			return summarize('color_lock', value);
		case 'kids_requests':
			if (!value.enabled) return t('profiles.v.off');
			return (value.categories || []).length ? shortList(value.categories.map((c) => t(`family.category.${c}`))) : t('family.requests.noKinds');
		case 'access_hours':
			if (!value.enabled) return t('profiles.accessHours.always');
			return (value.windows || []).length ? summarize('quiet_hours', value.windows) : t('profiles.accessHours.never');
		case 'setup_devices':
			if (!value.enabled) return t('profiles.v.off');
			return (value.kinds || []).length ? shortList(sortKinds(value.kinds).map((k) => kindLabel(k, true)), profileKinds().length) : t('profiles.kind.none');
		case 'kids_exceptions':
			return value.length ? t('profiles.v.exceptions', { count: value.length }) : t('profiles.v.noExceptions');
		case 'remote_control': {
			const parts = [levelLabel(value.default)];
			const n = Object.keys(value.domains || {}).length + Object.keys(value.entities || {}).length;
			if (n) parts.push(t('profiles.v.remoteExceptions', { count: n }));
			return parts.join(' · ');
		}
		case 'home_header': {
			const v = { ...HOME_HEADER_DEFAULT, ...value };
			const parts = HOME_PARTS.filter((p) => v[p]).map((p) => t(`profiles.home.${p}`));
			return t('profiles.home.summaryParts', { style: enumLabel('home_header', v.style), parts: parts.length ? parts.join(', ') : t('profiles.home.none') });
		}
		case 'columns':
			return t('profiles.v.columns', { count: value });
		case 'panel_sensors':
			return value.length ? shortList(value.map((x) => t(`profiles.panel.sensor.${x}`)), 6) : t('profiles.panel.sensorsNone');
		case 'panel_wake': {
			const base = (value.entities || []).length ? t('profiles.panel.wakeCount', { count: value.entities.length }) : t('profiles.panel.wakeNone');
			return value.proximity ? `${base} · ${t('profiles.panel.proximityShort')}` : base;
		}
		case 'panel_announce':
			if (!value.enabled) return t('profiles.v.off');
			return value.respect_night ? t('profiles.panel.announceSummaryNight', { n: value.volume_max }) : t('profiles.panel.announceSummary', { n: value.volume_max });
		case 'panel_brightness':
			if (value.mode === 'fixed') return `${t('profiles.panel.mode.fixed')} · ${value.level} %`;
			if (value.mode === 'lux' || value.mode === 'device_lux') return t('profiles.panel.luxSummary', { min: value.min, max: value.max });
			if (value.mode === 'schedule') return t('profiles.panel.scheduleSummary', { count: (value.schedule || []).length });
			return t('profiles.panel.mode.system');
		case 'panel_auto_home':
		case 'panel_dim':
		case 'panel_screensaver':
			return minutesLabel(value);
		default:
			break;
	}
	if (typeof value === 'boolean') return value ? t('profiles.v.on') : t('profiles.v.off');
	if (typeof value === 'string') return enumLabel(key, value);
	if (typeof value === 'number') return String(value);
	return JSON.stringify(value);
}

function shortList(parts, max = 3) {
	if (parts.length <= max) return parts.join(', ');
	return `${parts.slice(0, max).join(', ')} ${t('profiles.v.andMore', { count: parts.length - max })}`;
}

/** Index of a value in the rule's order (strict → loose), -1 when unknown. */
function orderIndex(rule, value) {
	return (rule?.order || []).findIndex((v) => same(v, value));
}

const LEVEL_RANK = { hidden: 0, view: 1, control: 2 };

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const RATE_MORE = ['max_actions', 'max_entities'];
const RATE_LESS = ['window_seconds', 'cooldown_seconds'];
const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

/** A rate limit (per device or for everything) allows more than before; off = looser. */
function rateLooser(before, after) {
	if (!isObj(after)) return true;
	for (const f of [...RATE_MORE, ...RATE_LESS]) {
		const b = before[f];
		const a = after[f];
		if (!isNum(b)) continue;
		if (!isNum(a)) return true;
		if (RATE_MORE.includes(f) ? a > b : a < b) return true;
	}
	return false;
}

/** Minutes of the week inside bedtime windows (end <= start runs into the next day, start == end = whole day). */
function quietMinutes(windows) {
	const out = new Set();
	for (const w of Array.isArray(windows) ? windows : []) {
		const [sh, sm] = String(w?.start).split(':').map(Number);
		const [eh, em] = String(w?.end).split(':').map(Number);
		if (![sh, sm, eh, em].every(Number.isInteger)) continue;
		const start = sh * 60 + sm;
		const end = eh * 60 + em;
		const length = end > start ? end - start : end - start + 1440;
		for (const day of w.days || []) {
			const d = WEEKDAYS.indexOf(day);
			if (d < 0) continue;
			for (let i = 0; i < length; i++) out.add((d * 1440 + start + i) % 10080);
		}
	}
	return out;
}

/** A minute of bedtime before is no longer bedtime after. */
function lostMinutes(before, after) {
	const a = quietMinutes(after);
	for (const m of quietMinutes(before)) if (!a.has(m)) return true;
	return false;
}

/** One kids rules object allows more than before (missing = inherited, unknown: counts unless strict). */
function kidsRulesLooser(before, after) {
	for (const f of new Set([...Object.keys(before), ...Object.keys(after)])) {
		const b = before[f];
		const a = after[f];
		if (same(b, a)) continue;
		if (f === 'blocked' || f === 'color_lock') {
			// false allows explicitly; dropping true (missing or null) inherits the unknown
			if (a === false || (a == null && b === true)) return true;
		} else if (f === 'blocked_types') {
			if (!Array.isArray(b) || !Array.isArray(a) || b.some((x) => !a.includes(x))) return true;
		} else if (f === 'quiet_hours') {
			if (!(f in before) || !(f in after) || lostMinutes(b, a)) return true;
		} else if (f === 'rate_limit') {
			if (!isObj(b) || rateLooser(b, a)) return true;
		} else if (f === 'volume_max') {
			if (!isNum(b) || !isNum(a) || a > b) return true;
		} else return true;
	}
	return false;
}

/** An exception for an area or device was added or allows more. */
function exceptionsLooser(before, after) {
	const byTarget = (v) => {
		const out = new Map();
		for (const x of Array.isArray(v) ? v : []) if (isObj(x?.rules)) out.set(JSON.stringify(x.target, Object.keys(x.target || {}).sort()), x.rules);
		return out;
	};
	const b = byTarget(before);
	const a = byTarget(after);
	for (const t of new Set([...b.keys(), ...a.keys()])) if (kidsRulesLooser(b.get(t) || {}, a.get(t) || {})) return true;
	return false;
}

/**
 * True when `after` is looser than `before` for a critical/sensitive rule
 * (along order; areas: more areas; when away: a higher level anywhere;
 * kids rules: less bedtime, a weaker rate limit, an exception that allows more).
 * Same rules as services/profiles/catalog.py loosens().
 */
export function loosens(rule, before, after) {
	if (!rule || (rule.security !== 'critical' && rule.security !== 'sensitive')) return false;
	// null switches the rate limit off.
	if (rule.key === 'kids_rate_limit') return isObj(before) && rateLooser(before, after);
	if (rule.key === 'kids_rate_limit_all') {
		if (!before?.enabled) return false;
		if (!after?.enabled) return true;
		return rateLooser(before, after);
	}
	if (before === null || before === undefined || after === null || after === undefined) return false;
	if (rule.key === 'kids_quiet_hours') return lostMinutes(before, after);
	if (rule.key === 'kids_exceptions') return exceptionsLooser(before, after);
	if (Array.isArray(rule.order)) {
		const a = orderIndex(rule, before);
		const b = orderIndex(rule, after);
		return a >= 0 && b > a;
	}
	if (rule.key === 'areas') {
		if (after.mode === 'all') return before.mode !== 'all';
		if (before.mode === 'all') return false;
		const has = (v, x) => (v.area_ids || []).includes(x);
		return (
			(after.area_ids || []).some((x) => !has(before, x)) ||
			(after.tags || []).some((x) => !(before.tags || []).includes(x)) ||
			(!!after.own_area && !before.own_area)
		);
	}
	if (rule.key === 'remote_control') {
		const lvl = (v, k, kind) => LEVEL_RANK[(v[kind] || {})[k] ?? v.default];
		if (LEVEL_RANK[after.default] > LEVEL_RANK[before.default]) return true;
		const keys = new Set([...Object.keys(before.domains || {}), ...Object.keys(after.domains || {})]);
		for (const k of keys) if (lvl(after, k, 'domains') > lvl(before, k, 'domains')) return true;
		const ents = new Set([...Object.keys(before.entities || {}), ...Object.keys(after.entities || {})]);
		for (const k of ents) if (lvl(after, k, 'entities') > lvl(before, k, 'entities')) return true;
		return false;
	}
	if (rule.key === 'kids_blocked_types') return (before || []).some((x) => !(after || []).includes(x));
	if (rule.key === 'access_hours') {
		// Off = always; more or other windows can mean more access.
		if (!before?.enabled) return false;
		if (!after?.enabled) return true;
		return JSON.stringify(after.windows || []) !== JSON.stringify(before.windows || []) && (after.windows || []).length >= (before.windows || []).length;
	}
	if (rule.key === 'setup_devices') {
		if (!after.enabled) return false;
		if (!before.enabled) return true;
		return (after.kinds || []).some((k) => !(before.kinds || []).includes(k));
	}
	return false;
}

/** Profile kinds in catalog order. */
export const PROFILE_KINDS = ['guest', 'child', 'panel', 'tv', 'house_sitter', 'babysitter', 'person'];

export function profileKinds() {
	return catalog.data?.profile_kinds || PROFILE_KINDS.map((id) => ({ id }));
}

export function sortKinds(kinds) {
	const order = profileKinds().map((k) => k.id);
	return [...(kinds || [])].sort((a, b) => order.indexOf(a) - order.indexOf(b));
}

/** Label of a profile kind (plural for "which kinds"); falls back to the i18n text. */
export function kindLabel(kind, plural = false) {
	const k = profileKinds().find((x) => x.id === kind);
	const text = k ? (plural ? k.plural : k.label) : null;
	if (text) return L(text);
	const key = `profiles.kind.${kind}`;
	const s = t(key);
	return s === key ? String(kind) : s;
}

export function kindIcon(kind) {
	return { guest: 'clock', child: 'child', panel: 'ipad', tv: 'tv', house_sitter: 'key', babysitter: 'stroller', person: 'user' }[kind] || 'user';
}

/** Readable device-type name for "When away" and kids rules. */
export function deviceTypeLabel(type) {
	const key = `profiles.types.${String(type).replace(':', '_')}`;
	const s = t(key);
	return s === key ? typeLabel(type) : s;
}

/**
 * One-sentence summary of a profile from its values (cards, no AI):
 * "Sees all areas · also when away · simple controls · bedtime Mon–Fri from 20:00".
 */
export function profileSummary(profile, ctx = {}) {
	if (!profile?.rules) return '';
	const r = profile.rules;
	const parts = [];
	if (r.areas) {
		if (r.areas.mode === 'all') parts.push(t('profiles.sum.allAreas'));
		else parts.push(t('profiles.sum.areas', { areas: formatValue('areas', r.areas, ctx) }));
	}
	parts.push(r.remote_access ? t('profiles.sum.away') : t('profiles.sum.homeOnly'));
	if (r.control_mode_max && r.control_mode_max !== 'full') parts.push(t(`profiles.sum.control_${r.control_mode_max}`));
	if (profile.child_protection && r.kids_quiet_hours?.length) {
		const w = r.kids_quiet_hours[0];
		parts.push(t('profiles.sum.bedtime', { when: daysLabel(w.days), start: w.start }));
	}
	if (r.kiosk) parts.push(t('profiles.sum.kiosk'));
	if (r.app_settings === 'hidden') parts.push(t('profiles.sum.noSettings'));
	return parts.join(' · ');
}

/** Summary sentence of a device from its effective rules (when the hub sends none). */
export function effectiveSummary(effective, ctx = {}) {
	const rules = {};
	for (const [k, e] of Object.entries(effective?.rules || {})) if (e && e.source !== 'not_applicable') rules[k] = e.value;
	return profileSummary({ rules, child_protection: !!effective?.profile?.child_protection }, ctx);
}

/** Localized name of a suggestion / profile text object or a plain string. */
export function textOf(v) {
	return typeof v === 'string' ? v : L(v);
}

/** Local end of an expiry preset (display only; the hub computes the real end). */
export function presetEnd(preset, now = new Date()) {
	const d = new Date(now);
	switch (preset) {
		case '1h':
			return new Date(d.getTime() + 3600_000);
		case '7d':
			return new Date(d.getTime() + 7 * 86400_000);
		case 'today': {
			const e = new Date(d);
			e.setHours(24, 0, 0, 0);
			return e;
		}
		case 'tomorrow_morning': {
			const e = new Date(d);
			if (d.getHours() >= 5) e.setDate(e.getDate() + 1);
			e.setHours(7, 0, 0, 0);
			return e;
		}
		default:
			return null;
	}
}

export function untilText(iso) {
	return formatWeekdayTime(iso);
}

/** Guest durations offered when pairing/inviting (days), at most 90 days per grant. */
export const GUEST_DAYS = [1, 3, 7, 14, 30];
