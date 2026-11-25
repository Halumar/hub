/**
 * Minimal i18n for the admin UI (DE/EN), Svelte 5 runes.
 *
 *   import { t, i18n } from '$lib/i18n/index.svelte.js';
 *   t('devices.title')                       -> "Devices" / "Geräte"
 *   t('devices.count', { count: 3 })         -> plural via Intl.PluralRules
 *   t('pairing.userName', { owner: 'Parents' }) -> "{owner}" interpolation
 *
 * {brand} is always available and comes from the generated brand.ts.
 * Plural entries are objects { one, other } (optionally zero) selected by
 * params.count. Missing keys fall back to English, then to the key itself.
 *
 * Language: user choice (localStorage "hub.lang": auto|en|de) – "auto" follows
 * Home Assistant (Ingress) and then the browser. Calling t() inside markup or
 * $derived is reactive: it reads the $state locale.
 */
import { BRAND } from '$lib/brand.ts';
import { readHass, watchHass } from '$lib/ha-context.js';
import en from './en/index.js';
import de from './de/index.js';

export const LOCALES = ['en', 'de'];
const DICTS = { en, de };
const STORAGE_KEY = 'hub.lang';

let _pref = $state('auto');
let _locale = $state('en');

function pick(tag) {
	const base = String(tag || '').toLowerCase().split(/[-_]/)[0];
	return LOCALES.includes(base) ? base : null;
}

function detect() {
	if (_pref !== 'auto') return _pref;
	const ha = pick(readHass()?.language);
	if (ha) return ha;
	if (typeof navigator !== 'undefined') {
		for (const l of navigator.languages || [navigator.language]) {
			const p = pick(l);
			if (p) return p;
		}
	}
	return 'en';
}

function apply() {
	_locale = detect();
	if (typeof document !== 'undefined' && document.documentElement.lang !== _locale) document.documentElement.lang = _locale;
}

export const i18n = {
	/** Effective language ('en' | 'de'). */
	get locale() {
		return _locale;
	},
	/** User preference ('auto' | 'en' | 'de'). */
	get preference() {
		return _pref;
	},
	/** @param {'auto'|'en'|'de'} pref */
	setPreference(pref) {
		_pref = pref === 'en' || pref === 'de' ? pref : 'auto';
		try {
			if (_pref === 'auto') localStorage.removeItem(STORAGE_KEY);
			else localStorage.setItem(STORAGE_KEY, _pref);
		} catch {
			/* private mode */
		}
		apply();
	},
	/** Read the stored preference and start following HA. Returns a cleanup. */
	init() {
		try {
			const stored = localStorage.getItem(STORAGE_KEY);
			if (stored === 'en' || stored === 'de') _pref = stored;
		} catch {
			/* ignore */
		}
		apply();
		return watchHass(() => _pref === 'auto' && apply());
	},
};

function lookup(dict, key) {
	let node = dict;
	for (const part of key.split('.')) {
		if (node == null || typeof node !== 'object') return undefined;
		node = node[part];
	}
	return node;
}

const pluralRules = {};
function plural(locale, count) {
	pluralRules[locale] ??= new Intl.PluralRules(locale);
	return pluralRules[locale].select(count);
}

/**
 * Translate a key.
 * @param {string} key dotted path, e.g. "areas.title"
 * @param {Record<string, any>} [params]
 * @returns {string}
 */
export function t(key, params = {}) {
	const locale = _locale;
	let value = lookup(DICTS[locale], key);
	if (value === undefined && locale !== 'en') value = lookup(DICTS.en, key);
	if (value === undefined) {
		if (import.meta.env?.DEV) console.warn(`[i18n] missing key: ${key}`);
		return key;
	}
	if (value && typeof value === 'object') {
		const count = Number(params.count ?? 0);
		value = (count === 0 && value.zero) || value[plural(locale, count)] || value.other || '';
	}
	const all = { brand: BRAND.name, ...params };
	return String(value).replace(/\{(\w+)\}/g, (m, name) => (all[name] ?? m).toString());
}

/** Intl formatters bound to the current UI language. */
export function fmtNumber(n) {
	return new Intl.NumberFormat(_locale).format(n);
}

/** All keys of a dictionary (for the completeness test). */
export function flatKeys(dict, prefix = '') {
	const out = [];
	for (const [k, v] of Object.entries(dict)) {
		const key = prefix ? `${prefix}.${k}` : k;
		if (v && typeof v === 'object' && !('other' in v)) out.push(...flatKeys(v, key));
		else out.push(key);
	}
	return out;
}

export { DICTS };
