// One Home Assistant user per device: display helpers.
// The companion integration names the HA user of a device exactly like this.
import { BRAND } from '$lib/brand.ts';

const GUEST_LABEL = 'Guest';
// Guest users named with the German label; the integration renames them to
// GUEST_LABEL at its next reconcile.
const GERMAN_GUEST_LABEL = 'Gast';
// After this long without a token the UI suggests checking the integration.
export const SETUP_SLOW_MS = 2 * 60 * 1000;

/** HA user name of a device: "<brand> · [Guest ][<owner> ]<device>". */
export function deviceUserName(client, guestLabel = GUEST_LABEL) {
	if (!client) return '';
	const parts = [];
	if (client.access === 'guest') parts.push(guestLabel);
	const owner = (client.owner_label || '').trim();
	const name = client.name || '';
	const lower = name.toLowerCase();
	const ownerLower = owner.toLowerCase();
	// Same rule as the integration: "Kitchen" of "Kitchen" and "Kitchen iPad" of "Kitchen" keep their name.
	if (owner && lower !== ownerLower && !lower.startsWith(`${ownerLower} `)) parts.push(owner);
	parts.push(name);
	return `${BRAND.name} · ${parts.join(' ')}`;
}

/**
 * Every name the HA user of a device can carry: for a guest also the name with
 * the German label "Gast", until the integration renames the user.
 */
export function deviceUserNames(client) {
	if (!client) return [];
	const names = [deviceUserName(client)];
	if (client.access === 'guest') names.push(deviceUserName(client, GERMAN_GUEST_LABEL));
	return names;
}

/** True while a device has waited longer than SETUP_SLOW_MS for its token. */
export function isSetupSlow(client, now = Date.now()) {
	if (client?.ha_token_status !== 'pending' || !client?.ha_token_pending_since) return false;
	const since = Date.parse(client.ha_token_pending_since);
	return Number.isFinite(since) && now - since > SETUP_SLOW_MS;
}
