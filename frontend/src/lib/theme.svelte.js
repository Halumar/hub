/**
 * Light/dark appearance of the admin UI (decision "admintheme": follow HA).
 * Preference in localStorage "hub.theme": auto | light | dark.
 * auto -> Home Assistant's dark mode (Ingress) -> prefers-color-scheme.
 * The result is written to <html data-theme>, which app.css keys on.
 */
import { readHass, watchHass } from '$lib/ha-context.js';

const STORAGE_KEY = 'hub.theme';
let _pref = $state('auto');
let _theme = $state('light');

function systemDark() {
	return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches;
}

function apply() {
	let dark;
	if (_pref === 'dark') dark = true;
	else if (_pref === 'light') dark = false;
	else dark = readHass()?.darkMode ?? systemDark();
	_theme = dark ? 'dark' : 'light';
	if (typeof document !== 'undefined' && document.documentElement.dataset.theme !== _theme) document.documentElement.dataset.theme = _theme;
}

export const theme = {
	/** Effective theme ('light' | 'dark'). */
	get current() {
		return _theme;
	},
	/** User preference ('auto' | 'light' | 'dark'). */
	get preference() {
		return _pref;
	},
	/** @param {'auto'|'light'|'dark'} pref */
	setPreference(pref) {
		_pref = pref === 'light' || pref === 'dark' ? pref : 'auto';
		try {
			if (_pref === 'auto') localStorage.removeItem(STORAGE_KEY);
			else localStorage.setItem(STORAGE_KEY, _pref);
		} catch {
			/* private mode */
		}
		apply();
	},
	/** Read the stored preference and follow HA / the OS. Returns a cleanup. */
	init() {
		try {
			const stored = localStorage.getItem(STORAGE_KEY);
			if (stored === 'light' || stored === 'dark') _pref = stored;
		} catch {
			/* ignore */
		}
		apply();
		const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
		const onMq = () => _pref === 'auto' && apply();
		mq?.addEventListener?.('change', onMq);
		const offHass = watchHass(onMq);
		return () => {
			mq?.removeEventListener?.('change', onMq);
			offHass();
		};
	},
};
