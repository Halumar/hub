/**
 * Several devices at once (POST /pairing/clients/bulk): one action per
 * run – assign a profile, set the own area, set or remove an adjustment.
 * The hub plans per device (dry_run first) and skips what is not possible
 * (guest + parental controls, fixed rule, boundary, not applicable).
 */

export const BULK_ACTIONS = ['assign_profile', 'set_own_area', 'set_override', 'remove_override'];

export function emptyChoices() {
	return {
		action: 'assign_profile',
		profileId: null,
		discard: false,
		areaId: '',
		rule: '',
		value: null,
		duration: 'permanent', // 1h | today | tomorrow_morning | 7d | date | permanent
		date: '',
		note: '',
	};
}

/** Body for POST /pairing/clients/bulk, or null when the choice is incomplete. */
export function bulkBody(c, clientIds, dryRun) {
	const body = { client_ids: [...clientIds], action: c.action, dry_run: !!dryRun };
	switch (c.action) {
		case 'assign_profile':
			if (!c.profileId) return null;
			body.profile_id = c.profileId;
			body.discard_overrides = !!c.discard;
			return body;
		case 'set_own_area':
			body.area_id = c.areaId || null;
			return body;
		case 'set_override':
			if (!c.rule || c.value === undefined) return null;
			body.rule = c.rule;
			body.value = c.value;
			if (c.duration === 'date') {
				if (!c.date) return null;
				body.expires_at = new Date(c.date).toISOString();
			} else if (c.duration !== 'permanent') body.expires = c.duration;
			if (c.note?.trim()) body.note = c.note.trim();
			return body;
		case 'remove_override':
			if (!c.rule) return null;
			body.rule = c.rule;
			return body;
		default:
			return null;
	}
}

/** Devices that can be selected (a pending deletion cannot change). */
export function selectable(client) {
	return !!client && !client.pending_deletion;
}

/** { changed, unchanged, skipped } of a bulk answer. */
export function counts(res) {
	const out = { changed: 0, unchanged: 0, skipped: 0 };
	for (const r of res?.results || []) out[r.status] = (out[r.status] || 0) + 1;
	return out;
}
