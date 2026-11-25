// Shared formatting and small DOM helpers for the admin UI.

import { t, i18n } from '$lib/i18n/index.svelte.js';

// Date/time formatting follows the UI language (i18n.locale) and the
// browser's time zone (the admin's local time).

/**
 * Relative, human-friendly "time ago" ("just now", "5 minutes ago", …);
 * older than a week: the local date.
 * @param {string|null|undefined} iso ISO timestamp
 * @returns {string}
 */
export function formatRelativeDate(iso) {
	if (!iso) return t('time.never');
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return String(iso);
	const diff = d.getTime() - Date.now();
	const abs = Math.abs(diff);
	if (abs < 60_000) return t('time.justNow');
	const rtf = new Intl.RelativeTimeFormat(i18n.locale, { numeric: 'auto' });
	if (abs < 3_600_000) return rtf.format(Math.round(diff / 60_000), 'minute');
	if (abs < 86_400_000) return rtf.format(Math.round(diff / 3_600_000), 'hour');
	if (abs < 7 * 86_400_000) return rtf.format(Math.round(diff / 86_400_000), 'day');
	return d.toLocaleDateString(i18n.locale);
}

/**
 * Absolute local date + time ("18.11.2026, 10:42"), "Never" when empty.
 * @param {string|null|undefined} iso ISO timestamp
 * @returns {string}
 */
export function formatDate(iso) {
	if (!iso) return t('common.never');
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return String(iso);
	return d.toLocaleString(i18n.locale, { dateStyle: 'medium', timeStyle: 'short' });
}

/**
 * Like formatDate, but "–" when empty (tables).
 * @param {string|null|undefined} iso ISO timestamp
 * @returns {string}
 */
export function formatDateTime(iso) {
	if (!iso) return '–';
	return formatDate(iso);
}

/**
 * Short local weekday + date + time: "Thu 19.11., 18:42" / "Do 19.11., 18:42".
 * @param {string|Date|null|undefined} value
 * @returns {string}
 */
export function formatWeekdayTime(value) {
	if (!value) return '';
	const d = value instanceof Date ? value : new Date(value);
	if (Number.isNaN(d.getTime())) return String(value);
	const loc = i18n.locale === 'de' ? 'de-CH' : i18n.locale;
	return d.toLocaleString(loc, { weekday: 'short', day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/**
 * Time left until an expiry: "", "expired", "2 days", "3 hours", "5 minutes".
 * @param {string|null|undefined} expiresAt ISO timestamp
 * @returns {string}
 */
export function formatTimeRemaining(expiresAt) {
	if (!expiresAt) return '';
	const diff = new Date(expiresAt).getTime() - Date.now();
	if (!(diff > 0)) return t('time.expired');
	const hours = Math.floor(diff / 3_600_000);
	if (hours >= 48) return t('time.days', { count: Math.floor(hours / 24) });
	if (hours >= 1) return t('time.hours', { count: hours });
	return t('time.minutes', { count: Math.max(1, Math.floor(diff / 60_000)) });
}

/**
 * Short duration label for a number of hours: "4 h", "1 d", "1 wk".
 * @param {number} hours
 * @returns {string}
 */
export function formatHoursShort(hours) {
	if (hours % 168 === 0) return t('time.durationShort.w', { count: hours / 168 });
	if (hours % 24 === 0) return t('time.durationShort.d', { count: hours / 24 });
	return t('time.durationShort.h', { count: hours });
}

/**
 * Duration in seconds formatted as "m:ss".
 * Source: src/routes/settings/+page.svelte `formatTime(seconds)`.
 * @param {number} seconds
 * @returns {string} e.g. "2:05"
 */
export function formatDuration(seconds) {
	const m = Math.floor(seconds / 60);
	const s = seconds % 60;
	return `${m}:${s.toString().padStart(2, '0')}`;
}

/**
 * Copy text to the clipboard, working on plain-http origins too.
 * navigator.clipboard only exists in secure contexts (https), so fall back
 * to a hidden textarea + document.execCommand('copy') elsewhere.
 * @param {string} text
 * @returns {Promise<boolean>} true when the copy succeeded
 */
export async function copyText(text) {
	if (navigator.clipboard?.writeText) {
		try {
			await navigator.clipboard.writeText(text);
			return true;
		} catch {
			// Fall through to the legacy path (e.g. permission denied).
		}
	}
	try {
		const ta = document.createElement('textarea');
		ta.value = text;
		// Keep it out of view without display:none (which would break select()).
		ta.style.position = 'fixed';
		ta.style.opacity = '0';
		document.body.appendChild(ta);
		ta.select();
		ta.setSelectionRange(0, ta.value.length);
		const ok = document.execCommand('copy');
		document.body.removeChild(ta);
		return ok;
	} catch {
		return false;
	}
}

/**
 * Resolve a client's display name from a list, falling back to a short id.
 * The `clients` array is passed in explicitly.
 * @param {string} clientId
 * @param {{ id: string, name?: string }[]} clients
 * @returns {string}
 */
export function clientName(clientId, clients) {
	const c = clients.find(c => c.id === clientId);
	return c ? c.name : clientId.substring(0, 8);
}

/**
 * Format a Date as the value of an <input type="datetime-local"> in the
 * browser's LOCAL time zone ("YYYY-MM-DDTHH:mm"). toISOString() would give
 * UTC, which the input then reads back as local time (off by the UTC offset).
 * @param {Date} d
 * @returns {string}
 */
export function toLocalInput(d) {
	const pad = (n) => String(n).padStart(2, '0');
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Trailing-edge debounce: bursts of calls (e.g. N broadcasts after a bulk
 * change) collapse into one call `ms` after the last one.
 * @template {(...args: any[]) => any} F
 * @param {F} fn
 * @param {number} [ms]
 * @returns {F & { cancel: () => void }}
 */
export function debounce(fn, ms = 200) {
	let timer = null;
	const debounced = (...args) => {
		if (timer) clearTimeout(timer);
		timer = setTimeout(() => {
			timer = null;
			fn(...args);
		}, ms);
	};
	debounced.cancel = () => {
		if (timer) clearTimeout(timer);
		timer = null;
	};
	return /** @type {any} */ (debounced);
}
