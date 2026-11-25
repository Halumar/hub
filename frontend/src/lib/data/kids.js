/**
 * Kids mode: rule objects, inheritance and readable summaries.
 *
 * A rule object exists on three levels (profile -> area -> entity). A missing
 * key is inherited from the more general level; null for rate_limit and
 * volume_max switches the rule off on that level. blocked_types only exists
 * on the profile and area level. Same model as the hub's services/kids_rules.py.
 */
import { t } from '$lib/i18n/index.svelte.js';

export const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
export const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri'];
export const WEEKEND = ['sat', 'sun'];

export const RULE_KEYS = ['blocked', 'blocked_types', 'quiet_hours', 'rate_limit', 'color_lock', 'volume_max'];

/** Keys a level may set. */
export function levelKeys(level) {
	return level === 'entity' ? RULE_KEYS.filter((k) => k !== 'blocked_types') : RULE_KEYS;
}

export const DEFAULT_RATE = { max_actions: 6, window_seconds: 60, cooldown_seconds: 300 };
export const DEFAULT_WINDOW = { days: [...WEEKDAYS], start: '20:00', end: '06:30' };
export const OVERRIDE_MINUTES = [15, 30, 60, 120];

/** Fallback when GET /api/profiles/defaults is not available. */
export const FALLBACK_TYPES = ['lock', 'alarm_control_panel', 'cover:garage', 'cover:gate', 'camera', 'media_player', 'climate', 'switch', 'fan', 'vacuum', 'siren'];

export function clone(value) {
	return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

/** Readable name of a device type (domain or domain:device_class). */
export function typeLabel(type) {
	const key = `kids.types.${String(type).replace(':', '_')}`;
	const label = t(key);
	return label === key ? type : label;
}

/** Effective value of `key` over the given levels (most specific first). */
export function effective(key, ...levels) {
	for (const rules of levels) {
		if (rules && Object.prototype.hasOwnProperty.call(rules, key)) return { value: rules[key], set: true };
	}
	return { value: undefined, set: false };
}

/** "Mo–Fr", "Sa, So", "Täglich" for a list of days. */
export function daysLabel(days) {
	const list = DAYS.filter((d) => days?.includes(d));
	if (list.length === 7) return t('kids.days.everyDay');
	if (list.length === 5 && WEEKDAYS.every((d) => list.includes(d))) return t('kids.days.weekdays');
	if (list.length === 2 && WEEKEND.every((d) => list.includes(d))) return t('kids.days.weekend');
	// Consecutive run, also across the week end (Sun–Thu): first–last
	if (list.length > 2) {
		for (let start = 0; start < 7; start++) {
			const run = Array.from({ length: list.length }, (_, i) => DAYS[(start + i) % 7]);
			if (run.every((d) => list.includes(d))) {
				return `${t(`kids.days.short.${run[0]}`)}–${t(`kids.days.short.${run[run.length - 1]}`)}`;
			}
		}
	}
	return list.map((d) => t(`kids.days.short.${d}`)).join(', ');
}

export function crossesMidnight(w) {
	return w.end <= w.start;
}

export function windowLabel(w) {
	if (w.start === w.end) return t('kids.quiet.wholeDay', { days: daysLabel(w.days) });
	return `${daysLabel(w.days)} ${w.start}–${w.end}`;
}

function minutes(seconds) {
	if (seconds % 60 === 0 && seconds >= 60) return t('kids.unit.min', { n: seconds / 60 });
	return t('kids.unit.sec', { n: seconds });
}

/** Short text of one rule value (for "Inherited: …" and summaries). */
export function summarize(key, value) {
	switch (key) {
		case 'blocked':
			return value === true ? t('kids.access.blocked') : value === false ? t('kids.access.allowed') : t('kids.access.normal');
		case 'blocked_types':
			return value?.length ? value.map(typeLabel).join(', ') : t('kids.types.none');
		case 'quiet_hours':
			return value?.length ? value.map(windowLabel).join(' · ') : t('kids.quiet.none');
		case 'rate_limit':
			return value
				? t('kids.rate.summary', { n: value.max_actions, window: minutes(value.window_seconds), pause: minutes(value.cooldown_seconds) })
				: t('kids.rate.off');
		case 'rate_limit_all':
			return value?.enabled
				? t('kids.rateAll.summary', {
						entities: value.max_entities,
						n: value.max_actions,
						window: minutes(value.window_seconds),
						pause: minutes(value.cooldown_seconds),
					})
				: t('kids.rate.off');
		case 'color_lock':
			return value ? t('kids.color.on') : t('kids.color.off');
		case 'volume_max':
			return value === null || value === undefined ? t('kids.volume.off') : t('kids.volume.summary', { n: value });
		default:
			return '';
	}
}

/** Number of own rules in an object (area/entity badges). */
export function ruleCount(rules) {
	return rules ? Object.keys(rules).filter((k) => RULE_KEYS.includes(k)).length : 0;
}

/** Child-friendly text of a decision (rule test, activity). */
export function reasonLabel(reason) {
	const key = `kids.reason.${reason}`;
	const label = t(key);
	return label === key ? reason : label;
}

export function levelLabel(level) {
	return level ? t(`kids.level.${level}`) : '';
}

/** Validation of a quiet-hours list for the editor (start/end, at least one day). */
export function windowsValid(list) {
	return (list || []).every((w) => w.days?.length && /^\d\d:\d\d$/.test(w.start) && /^\d\d:\d\d$/.test(w.end));
}

export function rateValid(r) {
	if (!r) return true;
	const ok = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;
	return ok(r.max_actions, 1, 100) && ok(r.window_seconds, 5, 3600) && ok(r.cooldown_seconds, 0, 86400);
}

/** Pause everything (kids_rate_limit_all): ranges of the contract. */
export function rateAllValid(r) {
	if (!r || !r.enabled) return true;
	const ok = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;
	return ok(r.max_actions, 1, 200) && ok(r.max_entities, 1, 50) && ok(r.window_seconds, 10, 3600) && ok(r.cooldown_seconds, 30, 86400);
}

/** 422 detail "rules.<field>" -> field key, else null. */
export function ruleErrorField(err) {
	const m = /^rules\.(\w+)$/.exec(err?.message || '');
	return m ? m[1] : null;
}
