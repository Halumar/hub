/**
 * Rule catalog of the profile model (contract data/profile-rules.json),
 * loaded once from GET /api/profile-rules. Labels, help texts, reason
 * sentences and enforcement texts come from the catalog in DE/EN; everything
 * else the editor needs (value choices) lives in $lib/profiles/format.js.
 *
 *   const cat = await loadCatalog();
 *   ruleOf('remote_access').label → { de, en }
 *   L(rule.label)                 → text in the UI language with {brand}
 */
import { api } from '$lib/api.js';
import { BRAND } from '$lib/brand.ts';
import { i18n } from '$lib/i18n/index.svelte.js';

let data = $state(null);
let error = $state('');
/** @type {Promise<any> | null} */
let pending = null;

export const catalog = {
	get data() {
		return data;
	},
	get error() {
		return error;
	},
};

/** Load the catalog (cached; a failed load is retried on the next call). */
export function loadCatalog() {
	if (data) return Promise.resolve(data);
	if (!pending) {
		pending = api
			.getProfileRules()
			.then((c) => {
				data = c;
				error = '';
				return c;
			})
			.catch((e) => {
				error = e.message || String(e);
				pending = null;
				throw e;
			});
	}
	return pending;
}

/** Text of a localized catalog object { de, en } with {brand} and params. */
export function L(text, params = {}) {
	if (!text) return '';
	const raw = typeof text === 'string' ? text : text[i18n.locale] ?? text.en ?? text.de ?? '';
	const all = { brand: BRAND.name, ...params };
	return String(raw).replace(/\{(\w+)\}/g, (m, name) => (all[name] ?? m).toString());
}

export function ruleOf(key) {
	return data?.rules?.find((r) => r.key === key) || null;
}

/** Groups in catalog order, each with its rules. */
export function groupsWithRules(cat = data) {
	if (!cat) return [];
	return (cat.groups || []).map((g) => ({ ...g, rules: (cat.rules || []).filter((r) => r.group === g.id) }));
}

export function groupOf(id) {
	return data?.groups?.find((g) => g.id === id) || null;
}

/** Admin sentence of a reason code; unknown codes get a neutral text. */
export function reasonText(code, params = {}) {
	const r = data?.reasons?.find((x) => x.code === code);
	return r?.admin ? L(r.admin, params) : '';
}

/**
 * How a rule is enforced on a device with the effective control path
 * `path` ('hub' | 'checked' | 'direct'): { id: 'ha'|'hub'|'app'|'ha_control',
 * label, help }. On the hub path the hub enforces (enforcement_with_hub_path),
 * on the hub and the checked path Home Assistant enforces operating
 * (enforcement_with_checked_path). "app" is the honest "Applies in {brand}",
 * ha/hub are "Enforced", ha_control "Control enforced".
 */
export function enforcementOf(rule, path = 'checked') {
	if (!rule) return null;
	const id =
		(path === 'hub' && rule.enforcement_with_hub_path) ||
		((path === 'hub' || path === 'checked') && rule.enforcement_with_checked_path) ||
		rule.enforcement;
	if (!id) return null;
	const e = data?.enforcement?.find((x) => x.id === id);
	return { id, label: e ? L(e.label) : id, help: e ? L(e.help) : '' };
}

/** Profile kinds that resolve to "hub" without a stored control path (like parental controls). */
export const HUB_DEFAULT_KINDS = ['guest', 'babysitter', 'house_sitter'];

/**
 * Home Assistant access of a profile body. The hub reports the resolver's value,
 * also its fallback when none is stored (then the rule is in `catalog_defaults`).
 * A guest device (`guest`) on a profile without a stored level is on "hub",
 * whatever the profile kind. Without any value: parental controls and the
 * guest kinds mean "hub", every other profile the catalog default.
 */
export function controlPathOf(body, { guest = false } = {}) {
	const v = body?.rules?.control_path;
	const stored = !!v && !(body?.catalog_defaults || []).includes('control_path');
	if (guest && !stored) return 'hub';
	if (v) return v;
	return body?.child_protection || HUB_DEFAULT_KINDS.includes(body?.kind) ? 'hub' : ruleOf('control_path')?.default || 'checked';
}

/** Platforms whose devices always hold their own key: "hub" resolves to "checked" there. */
export const KEY_PLATFORMS = ['tvos', 'android_tv', 'watchos', 'wearos'];

export function expiryPresets() {
	return data?.expiry_presets || [];
}

export function guestAccess() {
	return data?.guest_access || { default_hours: 24, max_hours: 2160, presets_hours: [24, 72, 168, 336, 720] };
}

export function limits() {
	return data?.limits || { max_profiles: 50, profile_name_max_length: 40, note_max_length: 200 };
}

/** Boundary entry of a rule for "guest", "child_protection" or "hub_only", or null. */
export function boundaryOf(rule, boundary) {
	return rule?.boundaries?.find((b) => b.boundary === boundary) || null;
}

export function boundaryInfo(id) {
	return data?.boundaries?.find((b) => b.id === id) || null;
}
