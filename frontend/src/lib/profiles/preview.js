/**
 * Reading a profile-change preview (profile-preview.response): whether a
 * device is on "Hub only" with remote access, the state in which it holds a
 * read-only Home Assistant key that can read every state. The hub says so per
 * device: hub_remote_now (right after the change) and hub_remote_at (the
 * earliest later moment, through any scheduled end of any rule or adjustment).
 */

import { api } from '$lib/api.js';

/**
 * { now, at } of a preview entry. A hub that does not send the two fields
 * gets a conservative reading: the warning shows whenever the change touches
 * the level or remote access and the device could end up with either half of
 * "Hub only with remote access" (`current`: the paired client).
 */
export function hubRemoteOf(d, current) {
	if (d && Object.prototype.hasOwnProperty.call(d, 'hub_remote_now')) return { now: !!d.hub_remote_now, at: d.hub_remote_at || null };
	const change = (rule) => (d?.changes || []).find((c) => c.rule === rule);
	const cp = change('control_path');
	const ra = change('remote_access');
	const restored = d?.ha_key === 'restored' && d?.remote_access === 'enabled';
	if (!cp && !ra && !restored) return { now: false, at: null };
	const path = cp ? cp.after?.value : current?.control_path ?? 'hub';
	let remote;
	if (ra) remote = ra.after?.value === true;
	else if (d?.remote_access === 'enabled' || d?.remote_access === 'disabled') remote = d.remote_access === 'enabled';
	else remote = current?.remote_allowed ?? true;
	const maybe = path === 'hub' || remote === true || cp?.after?.then === 'hub' || ra?.after?.then === true;
	return { now: maybe, at: null };
}

/** True when the device is or becomes "Hub only" with remote access. */
export function endsHubRemote(d, current) {
	const s = hubRemoteOf(d, current);
	return s.now || !!s.at;
}

/**
 * Extending a guest's access, asked of the hub first (PATCH /clients/{id}/extend with dry_run):
 * { now, at } as in previews, `unknown` when the hub gives no answer (then the caller warns
 * rather than blocks), `expires_at` the new end the hub would set.
 */
export async function previewExtend(clientId, data) {
	try {
		const r = await api.extendGuest(clientId, { ...data, dry_run: true });
		// Only a real dry-run answer counts; anything else is "no answer".
		if (r?.dry_run === true && Object.prototype.hasOwnProperty.call(r, 'hub_remote_now')) return { now: !!r.hub_remote_now, at: r.hub_remote_at || null, unknown: false, expires_at: r.expires_at || null };
	} catch {
		// No answer: unknown.
	}
	return { now: false, at: null, unknown: true, expires_at: null };
}

/** Phone and tablet platforms: guests and pairing PINs can only be used on these. */
export const PAIRABLE_GUEST_PLATFORMS = ['ios', 'ipados', 'android', 'android_tablet'];

/**
 * The hub's answer for a device paired now with `profile` (hub_remote_pairing): true when on
 * one of `platforms` it would be "Hub only" with remote access, as member or guest (`access`).
 * null when the hub does not send it (then the caller falls back to its own reading).
 */
export function pairingHubRemote(profile, access, platforms) {
	const map = profile?.hub_remote_pairing?.[access];
	if (!map) return null;
	return platforms.some((p) => map[p] === true);
}
