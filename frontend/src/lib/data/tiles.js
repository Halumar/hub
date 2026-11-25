// Tile settings per area entity.
//
// Pure helpers for the tile panel: the capabilities table comes from the hub
// (GET /api/tiles/capabilities = contract data/tile-capabilities.json), so the
// Admin UI offers exactly what the hub accepts. Preview helpers approximate the
// app cards (HubCard / DeviceStyle) – the apps remain the reference.

import {
	mdiAccount,
	mdiAirHumidifier,
	mdiBell,
	mdiBlinds,
	mdiCctv,
	mdiFan,
	mdiGauge,
	mdiGestureTapButton,
	mdiInformation,
	mdiLightbulb,
	mdiLock,
	mdiLockOpenVariant,
	mdiMotionSensor,
	mdiPalette,
	mdiRobot,
	mdiRobotMower,
	mdiRobotVacuum,
	mdiScriptText,
	mdiShieldHome,
	mdiSpeaker,
	mdiThermostat,
	mdiToggleSwitch,
	mdiWater,
	mdiWaterBoiler,
	mdiWeatherSunny,
} from '@mdi/js';

export const VARIANTS = ['compact', 'large', 'slider', 'action'];
export const ACCENTS = ['accent', 'light', 'heat', 'safe', 'sea', 'cool', 'media', 'neutral'];
export const FORM_FACTORS = ['phone', 'tablet', 'tv'];
export const HOLD_ACTIONS = ['default', 'more_info', 'none'];
/** Size (columns × rows) the Admin UI suggests for a variant. */
export const VARIANT_SIZE = { compact: [1, 1], large: [1, 2], slider: [2, 1], action: [1, 1] };
const ICON_RE = /^(mdi:[a-z0-9]+(-[a-z0-9]+)*|sf:[a-z0-9]+(\.[a-z0-9]+)*)$/;
const SF_RE = /^[a-z0-9]+(\.[a-z0-9]+)*$/;
const TAP_ORDER = ['default', 'toggle', 'more_info', 'run', 'none'];
const WRITABLE = ['icon', 'variant', 'secondary', 'tap_action', 'hold_action', 'confirm', 'accent', 'hidden_on'];

// JSON clone: also works on Svelte $state proxies (structuredClone does not).
const clone = (v) => JSON.parse(JSON.stringify(v));

export const domainOf = (entityId) => String(entityId || '').split('.')[0];

/** Row of the capabilities table for an entity (else the `_other` row). */
export function capRow(caps, entityId) {
	const rows = caps?.domains || [];
	const d = domainOf(entityId);
	return rows.find((r) => r.domains.includes(d)) || rows.find((r) => r.domains.includes('_other')) || null;
}

/** Tap actions offered for a row, in display order. */
export function tapActions(caps, row) {
	const allowed = new Set([...(caps?.always?.tap_actions || []), ...(row?.tap_actions || [])]);
	return TAP_ORDER.filter((a) => allowed.has(a));
}

/** Writable tile fields of a stored/delivered tile (drops hub outputs). */
export function writableTile(tile) {
	const out = {};
	for (const k of WRITABLE) {
		if (tile?.[k] !== undefined && tile[k] !== null) out[k] = clone(tile[k]);
	}
	return out;
}

/** Draft edited in the panel. */
export function draftFrom(entity) {
	return {
		display_name: entity.display_name || '',
		columns: entity.columns || 1,
		rows: entity.rows || 1,
		tile: writableTile(entity.tile),
	};
}

/** Tile object for the request: defaults removed; null when all default. */
export function tileBody(tile) {
	const out = {};
	for (const k of WRITABLE) {
		const v = tile?.[k];
		if (v === undefined || v === null) continue;
		if (k === 'variant' && v === 'compact') continue;
		if (k === 'tap_action' && v.action === 'default') continue;
		if (k === 'hold_action' && v.action === 'default') continue;
		if (k === 'secondary' && v.source === 'state') continue;
		if (k === 'confirm' && v === false) continue;
		if (k === 'hidden_on' && (!Array.isArray(v) || v.length === 0)) continue;
		if (k === 'icon' && v === '') continue;
		out[k] = clone(v);
	}
	return Object.keys(out).length ? out : null;
}

const sameJson = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
const normName = (s) => (s || '').trim() || null;

/** PUT body with the changed fields only (contracts rest/area-entity-update.request). */
export function payload(entity, draft) {
	const body = {};
	if (normName(draft.display_name) !== normName(entity.display_name)) body.display_name = normName(draft.display_name);
	if ((draft.columns || 1) !== (entity.columns || 1)) body.columns = draft.columns;
	if ((draft.rows || 1) !== (entity.rows || 1)) body.rows = draft.rows;
	const next = tileBody(draft.tile);
	if (!sameJson(next, tileBody(writableTile(entity.tile)))) body.tile = next;
	return body;
}

export const isDirty = (entity, draft) => !!entity && !!draft && Object.keys(payload(entity, draft)).length > 0;

export const validIcon = (v) => !v || ICON_RE.test(v);
export const validSf = (v) => SF_RE.test(v || '');

/** Variant the app will actually draw (fallback rules, README "Kachel-Einstellungen"). */
export function effectiveVariant(tile, row, cols, rows, sectionColumns = 2) {
	const v = tile?.variant || 'compact';
	if (!row?.variants?.includes(v)) return 'compact';
	const width = sectionColumns < 2 ? 1 : cols;
	if (v === 'slider' && width < 2) return 'compact';
	if (v === 'large' && width < 2 && rows < 2) return 'compact';
	return v;
}

/** Variant needs a bigger size than the draft has. */
export function sizeMismatch(tile, row, cols, rows, sectionColumns = 2) {
	const v = tile?.variant || 'compact';
	return row?.variants?.includes(v) && effectiveVariant(tile, row, cols, rows, sectionColumns) !== v;
}

/** Editor badges for a saved tile: [{key, params?, tone?}] (labels via i18n). */
export function badgeList(tile, targetsMissing = false) {
	const out = [];
	if (!tile) return out;
	if (tile.variant && tile.variant !== 'compact') out.push({ key: `tiles.variants.${tile.variant}`, tone: 'acc' });
	if (tile.icon) out.push({ key: 'tiles.badges.icon' });
	if (tile.accent) out.push({ key: 'tiles.badges.accent', color: tile.accent });
	if (tile.secondary && tile.secondary.source !== 'state') out.push({ key: 'tiles.badges.secondary' });
	if (tile.tap_action && tile.tap_action.action !== 'default') out.push({ key: 'tiles.badges.tap', tap: tile.tap_action });
	if (tile.confirm) out.push({ key: 'tiles.badges.confirm' });
	for (const f of tile.hidden_on || []) out.push({ key: 'tiles.badges.hidden', device: f, tone: 'warn' });
	if (targetsMissing) out.push({ key: 'tiles.targetMissing', tone: 'err' });
	return out;
}

// ── preview (approximation of HubCard / DeviceStyle) ─────────────────────────

/** Domain tint family (HubTint) as in DeviceStyle.tint. */
const DOMAIN_TINT = {
	light: 'light',
	switch: 'light',
	input_boolean: 'light',
	fan: 'cool',
	cover: 'sea',
	valve: 'sea',
	climate: 'heat',
	water_heater: 'heat',
	humidifier: 'sea',
	media_player: 'media',
	scene: 'accent',
	script: 'accent',
	button: 'accent',
	input_button: 'accent',
	lock: 'safe',
	alarm_control_panel: 'safe',
	vacuum: 'sea',
	lawn_mower: 'safe',
	sensor: 'soft-sea',
	binary_sensor: 'sea',
	person: 'safe',
	device_tracker: 'safe',
	siren: 'heat',
};

/** Default icon per domain (approximation of the app's DeviceStyle.symbol). */
export const domainIconPath = (entityId) => DOMAIN_ICON[domainOf(entityId)] || mdiInformation;

const DOMAIN_ICON = {
	light: mdiLightbulb,
	switch: mdiToggleSwitch,
	input_boolean: mdiToggleSwitch,
	fan: mdiFan,
	cover: mdiBlinds,
	valve: mdiWater,
	climate: mdiThermostat,
	water_heater: mdiWaterBoiler,
	humidifier: mdiAirHumidifier,
	media_player: mdiSpeaker,
	scene: mdiPalette,
	script: mdiScriptText,
	button: mdiGestureTapButton,
	input_button: mdiGestureTapButton,
	lock: mdiLock,
	alarm_control_panel: mdiShieldHome,
	camera: mdiCctv,
	vacuum: mdiRobotVacuum,
	lawn_mower: mdiRobotMower,
	sensor: mdiGauge,
	binary_sensor: mdiMotionSensor,
	person: mdiAccount,
	device_tracker: mdiAccount,
	weather: mdiWeatherSunny,
	automation: mdiRobot,
	siren: mdiBell,
};

const ACTIVE = new Set(['on', 'open', 'opening', 'playing', 'heat', 'cool', 'heat_cool', 'auto', 'dry', 'fan_only', 'unlocked', 'home', 'cleaning', 'armed_home', 'armed_away']);
const RUN_DOMAINS = new Set(['scene', 'script', 'button', 'input_button']);

export function isActive(domain, state) {
	if (RUN_DOMAINS.has(domain)) return false;
	if (domain === 'sensor') return true;
	return ACTIVE.has(String(state || '').toLowerCase());
}

/** SVG path for an `mdi:` value (curated set, hub output or picked paths). */
export function iconPath(icon, paths) {
	if (!icon || !icon.startsWith('mdi:')) return null;
	return paths?.get(icon.slice(4)) || null;
}

/** Level 0-100 for slider/level previews. */
function level(domain, attrs, on) {
	if (!on) return 0;
	if (domain === 'light' && typeof attrs.brightness === 'number') return Math.round((attrs.brightness / 255) * 100);
	if ((domain === 'cover' || domain === 'valve') && typeof attrs.current_position === 'number') return attrs.current_position;
	if (domain === 'fan' && typeof attrs.percentage === 'number') return attrs.percentage;
	if (domain === 'media_player' && typeof attrs.volume_level === 'number') return Math.round(attrs.volume_level * 100);
	return 60;
}

function fmtNum(v, locale) {
	const n = Number(v);
	if (!Number.isFinite(n)) return String(v ?? '');
	return new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(n);
}

/**
 * View model of one card for the preview.
 * @param {object} o {entity, tile, name, cols, rows, on (bool|null = live), dev, row, paths, t, locale, sensors, sectionColumns}
 */
export function cardModel(o) {
	const { entity, tile = {}, row, t, locale = 'en' } = o;
	const domain = domainOf(entity.entity_id);
	const st = entity.state || null;
	const attrs = st?.attributes || {};
	const liveOn = isActive(domain, st?.state);
	const on = RUN_DOMAINS.has(domain) ? false : o.on === null || o.on === undefined ? liveOn : o.on;
	const formFactor = o.dev === 'tv' ? 'tv' : 'phone';
	const hidden = (tile.hidden_on || []).includes(formFactor);
	const variant = effectiveVariant(tile, row, o.cols, o.rows, o.sectionColumns ?? 2);

	// icon: tile.icon (mdi) > hub icon_svg (HA icon) > domain default
	let path = null;
	if (row?.icon !== false) {
		path = iconPath(tile.icon, o.paths) || (tile.icon ? null : entity.tile?.icon_svg || null);
	}
	if (!path) path = domain === 'lock' && st?.state === 'unlocked' ? mdiLockOpenVariant : DOMAIN_ICON[domain] || mdiInformation;

	// tint: accent (only when active) > domain tint; locks/alarm always tinted
	let tint = null;
	const accentOk = row?.accent !== false;
	if (domain === 'lock') tint = st?.state === 'unlocked' ? 'heat' : 'safe';
	else if (domain === 'sensor') tint = accentOk && tile.accent ? tile.accent : 'soft-sea';
	else if (RUN_DOMAINS.has(domain)) tint = accentOk && tile.accent ? tile.accent : null;
	else if (on) tint = accentOk && tile.accent ? tile.accent : DOMAIN_TINT[domain] || 'neutral';
	if (tint === 'neutral') tint = null;

	const lvl = level(domain, attrs, on);
	const unit = attrs.unit_of_measurement ? ` ${attrs.unit_of_measurement}` : '';
	let status;
	let value = '';
	if (st?.state === 'unavailable') status = t('tiles.status.unavailable');
	else if (RUN_DOMAINS.has(domain)) status = t('tiles.status.run');
	else if (domain === 'light' || domain === 'fan') {
		status = on ? t('tiles.status.onLevel', { level: lvl }) : t('tiles.status.off');
		value = on ? `${lvl} %` : '';
	} else if (domain === 'cover' || domain === 'valve') {
		status = on ? t('tiles.status.open', { level: lvl }) : t('tiles.status.closed');
		value = on ? `${lvl} %` : '';
	} else if (domain === 'media_player') status = on ? t('tiles.status.playing') : t('tiles.status.paused');
	else if (domain === 'lock') status = st?.state === 'unlocked' ? t('tiles.status.unlocked') : t('tiles.status.locked');
	else if (domain === 'climate' || domain === 'water_heater') {
		const temp = attrs.current_temperature != null ? `${fmtNum(attrs.current_temperature, locale)} °` : '–';
		status = on ? t('tiles.status.heating', { temp }) : t('tiles.status.off');
		value = attrs.temperature != null ? `${fmtNum(attrs.temperature, locale)} °` : '';
	} else if (domain === 'sensor') status = `${fmtNum(st?.state, locale)}${unit}`;
	else status = on ? t('tiles.status.on') : t('tiles.status.off');

	// secondary line
	let secondary = null;
	const sec = tile.secondary;
	if (sec?.source === 'last_changed') secondary = t('tiles.status.since');
	else if (sec?.source === 'attribute') {
		const label = t(`tiles.attributes.${sec.attribute}`);
		let v = attrs[sec.attribute];
		if (sec.attribute === 'brightness' && typeof v === 'number') v = `${Math.round((v / 255) * 100)} %`;
		else if (sec.attribute === 'volume_level' && typeof v === 'number') v = `${Math.round(v * 100)} %`;
		else if (typeof v === 'number') v = fmtNum(v, locale);
		secondary = v === undefined || v === null ? null : `${label} ${v}`;
	} else if (sec?.source === 'entity') {
		const s = (o.sensors || []).find((x) => x.entity_id === sec.entity_id);
		const st2 = s ? { state: s.state, unit: s.unit } : entity.tile?.secondary_state ? { state: entity.tile.secondary_state.state, unit: entity.tile.secondary_state.attributes?.unit_of_measurement } : null;
		secondary = st2 ? `${fmtNum(st2.state, locale)}${st2.unit ? ` ${st2.unit}` : ''}` : null;
	}
	const bigValue = sec?.source === 'entity' && secondary ? secondary : value || status;

	return {
		entityId: entity.entity_id,
		domain,
		name: o.name,
		path,
		tint,
		variant,
		status,
		value: variant === 'slider' ? '' : value,
		secondary: sec?.source === 'none' ? null : secondary,
		bigValue,
		level: lvl,
		on,
		hidden,
		wide: (o.sectionColumns ?? 2) >= 2 && o.cols >= 2,
		tall: o.rows >= 2,
	};
}

/** Text of the tap action for the summary / TV hint line. */
export function tapText(tile, row, domain, dev, t, targetName) {
	const a = tile?.tap_action?.action || 'default';
	const allowed = a === 'default' || ['more_info', 'none'].includes(a) || row?.tap_actions?.includes(a);
	const action = allowed ? a : 'default';
	if (action === 'run') return t('tiles.status.tap.run', { target: targetName || tile.tap_action.target_entity_id });
	if (action === 'default') {
		if (RUN_DOMAINS.has(domain)) return t('tiles.status.tap.default_scene');
		if (dev === 'tv') return ['switch', 'input_boolean'].includes(domain) ? t('tiles.status.tap.toggle') : t('tiles.status.tap.details');
		return t('tiles.status.tap.more_info');
	}
	return t(`tiles.status.tap.${action === 'more_info' ? 'more_info' : action}`);
}
