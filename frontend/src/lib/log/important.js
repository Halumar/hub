/**
 * "Recent important" on the overview: the last important log entries
 * – alarms, kids' requests, guests, new devices, profile changes – and
 * the log filter each entry opens. Which entries count is decided by the hub
 * (data/events.json `important`, entry field `important`); the card asks once
 * with the filter important=true.
 */
import { freshFilters } from '$lib/log/state.svelte.js';

/** Kids' requests (activity entries of the family manager). */
export const REQUEST_KINDS = ['kids_release_requested', 'kids_request_answered', 'kids_request_expired'];

export const IMPORTANT_LIMIT = 6;

export function isImportant(e) {
	return Boolean(e?.important);
}

/** Kind of an important entry for the icon: alarm | request | guest | device | profile. */
export function groupOf(e) {
	if (e.category === 'alarm') return 'alarm';
	if (REQUEST_KINDS.includes(e.kind)) return 'request';
	const k = e.event_kind || e.kind;
	if (k?.startsWith('guest_')) return 'guest';
	if (k === 'device_paired') return 'device';
	return 'profile';
}

/** Newest important entries of the last 7 days (one request). */
export async function loadImportant(api, limit = IMPORTANT_LIMIT) {
	const r = await api.getLog({ range: '7d', important: true, limit });
	return (r?.entries || []).filter(isImportant);
}

/** Filters of the log tab that show this entry (and its neighbours). */
export function logFiltersFor(e, mobile = false) {
	const f = { ...freshFilters(mobile), range: '7d' };
	const g = groupOf(e);
	f.category = g === 'request' ? 'activity' : g === 'alarm' ? 'alarm' : 'change';
	if (e.client_id) f.client_id = e.client_id;
	else if (g === 'profile' && e.profile_id) f.profile_id = e.profile_id;
	return f;
}
