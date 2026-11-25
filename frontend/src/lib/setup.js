/**
 * Setup assistant: steps, done detection and the auto-open rule.
 *
 * The hub stores only the progress (GET/PUT /admin/setup-assistant); whether
 * a step is done is always derived from the live data, so steps the admin
 * already did elsewhere are recognized.
 */

export const STEPS = ['integration', 'connection', 'urls', 'area', 'device', 'done'];

/** Minimum Home Assistant version. */
export const MIN_HA_VERSION = '2025.11';

/**
 * My Home Assistant link that starts the config flow of the integration.
 * The domain comes from the hub (GET /admin/integration-status, ha_domain);
 * unknown → the integrations page.
 */
export function myHaAddIntegration(domain) {
	return domain
		? `https://my.home-assistant.io/redirect/config_flow_start/?domain=${encodeURIComponent(domain)}`
		: 'https://my.home-assistant.io/redirect/integrations/';
}

/**
 * My Home Assistant link that opens the integration's repository in the
 * Marketplace (Home Assistant 2026.11 and newer) or in HACS (older versions).
 * `owner` is the GitHub organization of the integration.
 */
export function myHaIntegrationRepository(owner) {
	return `https://my.home-assistant.io/redirect/hacs_repository/?owner=${encodeURIComponent(owner)}&repository=integration&category=integration`;
}

/** Installation guide (Documentation tab of the app), below BRAND.urls.source. */
export const INSTALL_GUIDE_PATH = '/hub/blob/main/halumar/DOCS.md#installation'; // brandlint: allow (app folder = slug)

/**
 * Public store link of the app. While it is a [[placeholder]] the setup
 * assistant shows no download button (the apps are not in a public store yet).
 */
export const APP_STORE_URL = '[[app-store-url]]';

export function isPlaceholderUrl(url) {
	return !url || /^\[\[.*\]\]$/.test(url);
}

/** Compare "YYYY.M[.x]" versions; null when unknown or unparsable. */
export function haVersionSupported(version, min = MIN_HA_VERSION) {
	const parse = (v) => String(v || '').match(/^(\d{4})\.(\d{1,2})/)?.slice(1).map(Number);
	const a = parse(version);
	const b = parse(min);
	if (!a || !b) return null;
	return a[0] > b[0] || (a[0] === b[0] && a[1] >= b[1]);
}

/**
 * @param {{ integration: any, token: any, haUrls: any, areas: any[], devices: any[] }} d
 * @returns {Record<string, boolean>}
 */
export function doneSteps({ integration, token, haUrls, areas, devices }) {
	const connected = !!integration?.connected;
	const members = (devices || []).filter((c) => !c.pending_deletion && c.access !== 'guest');
	const done = {
		integration: connected,
		connection: connected && token?.has_token !== false && haVersionSupported(integration?.ha_version) !== false,
		urls: !!(haUrls?.ha_internal_url || haUrls?.ha_internal_url_detected),
		area: (areas || []).length > 0,
		device: members.length > 0,
	};
	done.done = STEPS.slice(0, -1).every((s) => done[s]);
	return done;
}

/** First step that is not done (the summary when everything is). */
export function firstOpenStep(done) {
	return STEPS.find((s) => s !== 'done' && !done[s]) || 'done';
}

/**
 * Opens by itself once on a fresh hub: never opened before and the basics
 * (an area and a device) are still missing. An unknown state never opens it.
 */
export function shouldAutoOpen(state, { areas, devices }) {
	if (state?.status !== 'new') return false;
	const members = (devices || []).filter((c) => !c.pending_deletion && c.access !== 'guest');
	return !((areas || []).length > 0 && members.length > 0);
}
