/**
 * Halumar API Client
 * 
 * Detects HA Ingress base path automatically.
 * When running under Ingress, the backend injects window.__INGRESS_PATH.
 * API calls go to <ingress_path>/api/... so HA Supervisor proxies them correctly.
 *
 * Outside Ingress the admin session cookie authenticates (CSRF header on writes).
 */

import { t } from '$lib/i18n/index.svelte.js';

function getBasePath() {
	if (typeof window !== 'undefined' && window.__INGRESS_PATH) {
		return `${window.__INGRESS_PATH}/api`;
	}
	return '/api';
}

/**
 * Resolve an image src path for the current environment.
 * Backend returns paths like "/images/filename.png".
 * Under Ingress, these must be prefixed with the ingress path so the
 * HA Supervisor proxy routes them to Halumar instead of HA core.
 * External URLs (http://) and null values are returned as-is.
 */
export function resolveImageSrc(src) {
	if (!src) return null;
	// Already an absolute URL - return as-is
	if (src.startsWith('http://') || src.startsWith('https://')) return src;
	// Relative path like /images/... - prefix with ingress path if available
	if (typeof window !== 'undefined' && window.__INGRESS_PATH) {
		return `${window.__INGRESS_PATH}${src}`;
	}
	return src;
}

/**
 * Read a cookie value by name.
 */
function getCookie(name) {
	if (typeof document === 'undefined') return null;
	const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
	return match ? decodeURIComponent(match[1]) : null;
}

function getAuthHeaders(method = 'GET') {
	const headers = { 'Content-Type': 'application/json' };
	// CSRF token for state-changing requests (cookie-based auth)
	if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
		const csrf = getCookie('hub_csrf');
		if (csrf) {
			headers['X-CSRF-Token'] = csrf;
		}
	}
	return headers;
}

// --- Resilience config -------------------------------------------------------

// Max number of retries (in addition to the initial attempt).
const MAX_RETRIES = 3;
// Exponential backoff base in ms -> 200, 400, 800 for attempts 1, 2, 3.
const BACKOFF_BASE_MS = 200;
// Status codes that are safe to retry even for non-idempotent (write) methods,
// because they indicate the request likely never reached/was processed by the app.
const WRITE_RETRYABLE_STATUS = [502, 503, 504];
// Idempotent methods can always be retried on any 5xx.
const IDEMPOTENT_METHODS = ['GET', 'HEAD', 'OPTIONS'];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Signal to the UI that the session has expired (HTTP 401) so it can prompt
 * the user to re-login. We dispatch a catchable custom event on window; the
 * app can listen via window.addEventListener('hub:session-expired', ...).
 */
function notifySessionExpired() {
	if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
		window.dispatchEvent(new CustomEvent('hub:session-expired'));
	}
}

/**
 * Decide whether a failed attempt should be retried.
 * - 401 is NOT retried (auth/session issue) and never transient.
 * - Other 4xx are NOT retried (validation/auth — retrying won't help).
 * - GET/HEAD/OPTIONS retry on any 5xx and on network errors (idempotent).
 * - Write methods (POST/PUT/PATCH/DELETE) only retry on gateway-style 5xx
 *   (502/503/504), where the request almost certainly wasn't applied.
 *   Network errors are NOT retried for writes: the request may have been
 *   processed before the response got lost (e.g. an Ingress proxy timeout),
 *   and a retry would create duplicate notifications, areas, copies or PINs.
 */
function isRetryable(method, status) {
	if (status === null) return IDEMPOTENT_METHODS.includes(method); // network error / no response
	if (status < 500) return false;   // includes 401 and all other 4xx
	if (IDEMPOTENT_METHODS.includes(method)) return true; // any 5xx for GET-like
	return WRITE_RETRYABLE_STATUS.includes(status);       // limited 5xx for writes
}

/** Parse a successful response; anything that is not JSON is an error. */
async function parseOk(res) {
	if (res.status === 204) return null;
	const type = res.headers.get('content-type') || '';
	if (!type.includes('json')) {
		// An unknown GET path, also below /api, is answered with the SPA HTML (200).
		throw new Error(`Unexpected response from the add-on (HTTP ${res.status}, ${type || 'no content type'})`);
	}
	return res.json();
}

async function errorFromResponse(res) {
	const error = await res.json().catch(() => ({ detail: res.statusText }));
	const detail = error?.detail;
	const coded = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : null;
	const message = typeof detail === 'string'
		? detail
		: Array.isArray(detail) ? detail.map((d) => d?.msg || String(d)).join('; ') : coded?.message || coded?.code || '';
	// Body limits of the hub (413 too large, 408 too slow): its own text is English.
	const limit = res.status === 413 ? t('common.bodyTooLarge') : res.status === 408 ? t('common.bodyTooSlow') : '';
	const err = new Error(limit || message || `HTTP ${res.status}`);
	err.status = res.status;
	// Profile API: { detail: { code: "override.fixed" | "profile_in_use" | …, message } }
	err.code = coded?.code || (typeof detail === 'string' && /^[a-z_]+(\.[a-z_]+)*$/.test(detail) ? detail : null);
	// FastAPI 422: [{loc, msg, type}] – kept for field-level errors.
	err.detail = Array.isArray(detail) ? detail : null;
	// Family manager: 409 already_answered carries the answered request.
	err.request = coded?.request || null;
	// Machine-readable reason next to a text detail (e.g. admin_ingress_only).
	err.reason = typeof error?.reason === 'string' ? error.reason : null;
	return err;
}

/**
 * @param {string} method
 * @param {string} path
 * @param {any} [data]
 * @param {{ authEvent?: boolean }} [options] authEvent=false keeps a 401 from
 *   being reported as an expired session (login / session probe).
 */
/** A dry run (preview) that does not answer within this time counts as "no answer". */
export const DRY_RUN_TIMEOUT_MS = 10_000;

async function request(method, path, data = null, { authEvent = true } = {}) {
	const opts = {
		method,
		headers: getAuthHeaders(method),
		credentials: 'same-origin',  // Send session cookies
	};
	if (data) opts.body = JSON.stringify(data);
	// Dry runs get a deadline, so a dialog waiting for one never stays disabled for good.
	const dryRun = path.includes('dry_run=true') || data?.dry_run === true;
	if (dryRun) opts.signal = AbortSignal.timeout(DRY_RUN_TIMEOUT_MS);

	const url = `${getBasePath()}${path}`;
	let lastError = null;

	// Attempt 0 is the initial try; attempts 1..MAX_RETRIES are retries.
	for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
		let res;
		try {
			res = await fetch(url, opts);
		} catch (networkErr) {
			if (dryRun && (networkErr?.name === 'TimeoutError' || networkErr?.name === 'AbortError')) {
				const err = new Error('The hub did not answer the preview in time.');
				err.code = 'dry_run_timeout';
				throw err;
			}
			// Network/transport failure (no HTTP response).
			lastError = networkErr;
			if (attempt < MAX_RETRIES && isRetryable(method, null)) {
				await sleep(BACKOFF_BASE_MS * 2 ** attempt);
				continue;
			}
			throw new Error(`Network error: the add-on did not answer (${networkErr?.message || 'request failed'})`);
		}

		if (res.ok) return parseOk(res);

		// Surface session expiry so the UI can prompt re-login. Not retried.
		if (res.status === 401 && authEvent) {
			notifySessionExpired();
		}

		// Retry transient HTTP failures with exponential backoff.
		if (attempt < MAX_RETRIES && isRetryable(method, res.status)) {
			await sleep(BACKOFF_BASE_MS * 2 ** attempt);
			continue;
		}

		// Non-retryable or out of retries: throw a descriptive error.
		throw await errorFromResponse(res);
	}

	// Should be unreachable, but keep a safety net for the last network error.
	throw lastError || new Error('Request failed');
}

async function upload(path, file) {
	const formData = new FormData();
	formData.append('file', file);

	const uploadHeaders = {};
	// CSRF token for uploads (state-changing request)
	const csrf = getCookie('hub_csrf');
	if (csrf) {
		uploadHeaders['X-CSRF-Token'] = csrf;
	}

	let res;
	try {
		res = await fetch(`${getBasePath()}${path}`, {
			method: 'POST',
			headers: uploadHeaders,
			body: formData,
			credentials: 'same-origin',
		});
	} catch (networkErr) {
		throw new Error(`Network error: the add-on did not answer (${networkErr?.message || 'upload failed'})`);
	}

	if (!res.ok) {
		if (res.status === 401) notifySessionExpired();
		throw await errorFromResponse(res);
	}

	return parseOk(res);
}

const enc = encodeURIComponent;

/** "?a=1&b=2" from the non-empty values, "" when there are none. */
function query(params) {
	const q = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') q.set(k, String(v));
	const s = q.toString();
	return s ? `?${s}` : '';
}

const dry = (opts) => (opts?.dryRun ? '?dry_run=true' : '');

export const api = {
	// Quick access for widgets, controls and Siri
	getQuickAccess: () => request('GET', '/quick-access'),
	setQuickAccess: (entityId, data) => request('PUT', `/quick-access/${encodeURIComponent(entityId)}`, data),
	// Areas
	getAreas: (params = {}) => {
		const query = new URLSearchParams(params).toString();
		return request('GET', `/areas${query ? '?' + query : ''}`);
	},
	getArea: (id) => request('GET', `/areas/${id}`),
	getRoomClimate: (id) => request('GET', `/areas/${id}/room-climate`),
	setRoomClimate: (id, data) => request('PUT', `/areas/${id}/room-climate`, data),
	createArea: (data) => request('POST', '/areas', data),
	updateArea: (id, data) => request('PUT', `/areas/${id}`, data),
	patchArea: (id, data) => request('PATCH', `/areas/${id}`, data),
	deleteArea: (id) => request('DELETE', `/areas/${id}`),
	deleteAllAreas: () => request('DELETE', '/areas'),
	// Whole order in one step (every area exactly once).
	reorderAreas: (areaIds) => request('PUT', '/areas/order', { area_ids: areaIds }),
	copyArea: (id) => request('POST', `/areas/${id}/copy`),

	// Area Images (card, bg-light, bg-dark)
	uploadCardImage: (areaId, file) => upload(`/areas/${areaId}/image/card`, file),
	uploadBgLight: (areaId, file) => upload(`/areas/${areaId}/image/bg-light`, file),
	uploadBgDark: (areaId, file) => upload(`/areas/${areaId}/image/bg-dark`, file),
	deleteCardImage: (areaId) => request('DELETE', `/areas/${areaId}/image/card`),
	deleteBgLight: (areaId) => request('DELETE', `/areas/${areaId}/image/bg-light`),
	deleteBgDark: (areaId) => request('DELETE', `/areas/${areaId}/image/bg-dark`),

	// Area Sections
	createSection: (areaId, data) => request('POST', `/areas/${areaId}/sections`, data),
	getSections: (areaId) => request('GET', `/areas/${areaId}/sections`),
	updateSection: (areaId, sectionId, data) => request('PUT', `/areas/${areaId}/sections/${sectionId}`, data),
	deleteSection: (areaId, sectionId) => request('DELETE', `/areas/${areaId}/sections/${sectionId}`),
	reorderSections: (areaId, sectionIds) => request('PUT', `/areas/${areaId}/sections/order`, { section_ids: sectionIds }),

	// Area Entities
	addEntity: (areaId, data) => request('POST', `/areas/${areaId}/entities`, data),
	updateEntity: (areaId, entityId, data) => request('PUT', `/areas/${areaId}/entities/${entityId}`, data),
	removeEntity: (areaId, entityId) => request('DELETE', `/areas/${areaId}/entities/${entityId}`),
	updateLayout: (areaId, items) => request('PUT', `/areas/${areaId}/layout`, { items }),

	// Tile settings
	getTileCapabilities: () => request('GET', '/tiles/capabilities'),
	getTileSuggestions: (areaId, entityId) => request('GET', `/areas/${areaId}/entities/${entityId}/tile/suggestions`),

	// Area Widgets
	getWidgets: (areaId) => request('GET', `/areas/${areaId}/widgets`),
	createWidget: (areaId, data) => request('POST', `/areas/${areaId}/widgets`, data),
	updateWidget: (areaId, widgetId, data) => request('PUT', `/areas/${areaId}/widgets/${widgetId}`, data),
	deleteWidget: (areaId, widgetId) => request('DELETE', `/areas/${areaId}/widgets/${widgetId}`),

	// HA Integration
	getHAEntities: (domain = null) => {
		const query = domain ? `?domain=${domain}` : '';
		return request('GET', `/ha/entities${query}`);
	},
	getHAEntity: (entityId) => request('GET', `/ha/entities/${entityId}`),
	getHAAreas: () => request('GET', '/ha/areas'),
	getHADevices: () => request('GET', '/ha/devices'),
	getHADomains: () => request('GET', '/ha/domains'),

	// Sync
	syncFromHA: (areaIds = null, includeEntities = false, domains = null) => request('POST', '/sync/from-ha', {
		area_ids: areaIds,
		include_entities: includeEntities,
		domains: domains,
	}),
	syncAreaEntities: (areaId, domains = null) => request('POST', `/sync/area/${areaId}/entities`, {
		domains: domains,
	}),

	// Export/Import
	exportAreas: () => request('GET', '/export/areas'),
	exportArea: (id) => request('GET', `/export/areas/${id}`),
	importAreas: (data, overwrite = false) => request('POST', '/import/areas', { areas: data, overwrite }),

	// Auth / Session
	login: (username, password) => request('POST', '/auth/login', { username, password }, { authEvent: false }),
	logout: () => request('POST', '/auth/logout', null, { authEvent: false }),
	checkSession: () => request('GET', '/auth/session', null, { authEvent: false }),

	// API Token
	checkApiToken: () => request('GET', '/auth/token'),
	regenerateApiToken: () => request('POST', '/auth/token/regenerate'),

	// Integration Token (dedicated token for the companion HA integration;
	// auto-rotated + announced via Supervisor discovery on every add-on start)
	checkIntegrationToken: () => request('GET', '/auth/integration-token'),
	regenerateIntegrationToken: () => request('POST', '/auth/integration-token/regenerate'),

	// Pairing & Clients
	generatePairingPin: (data = null) => request('POST', '/pairing/generate', data),
	getPairingStatus: () => request('GET', '/pairing/status'),
	cancelPairing: () => request('DELETE', '/pairing/cancel'),
	// Setting up a device from a paired device: running sessions, cancel, option
	getOnboardingSessions: () => request('GET', '/pairing/onboarding'),
	cancelOnboarding: (id) => request('DELETE', `/pairing/onboarding/${encodeURIComponent(id)}`),
	getPairedClients: () => request('GET', '/pairing/clients'),
	getClient: (clientId) => request('GET', `/pairing/clients/${clientId}`),
	getClientActivity: (clientId, limit) =>
		request('GET', `/pairing/clients/${clientId}/activity${limit ? `?limit=${limit}` : ''}`),
	// { name, owner_label ('' clears) } – everything else is a profile rule
	updateClient: (clientId, data) => request('PATCH', `/pairing/clients/${clientId}`, data),
	deleteAllClients: () => request('DELETE', '/pairing/clients'),
	/** removal: null (online / default) | 'on_reconnect' | 'immediate' (offline device) */
	deleteClient: (clientId, removal = null) =>
		request('DELETE', `/pairing/clients/${clientId}/permanent${removal ? `?ha_user_removal=${encodeURIComponent(removal)}` : ''}`),
	revokeClient: (clientId) => request('DELETE', `/pairing/clients/${clientId}`),

	// Area tags (for the rule "Visible areas")
	getAvailableTags: () => request('GET', '/tags'),

	// Open guest invitations of member devices (never the PIN)
	getGuestInvites: () => request('GET', '/pairing/guest-invites'),
	cancelGuestInvite: (id) => request('DELETE', `/pairing/guest-invites/${encodeURIComponent(id)}`),
	// Profiles: catalog, profiles, suggestions, defaults, per device
	// effective rules and adjustments. Writes with { dryRun: true } answer the
	// preview (202, profile-preview.response) and save nothing.
	getProfileRules: () => request('GET', '/profile-rules'),
	listProfiles: () => request('GET', '/profiles'),
	getProfile: (id) => request('GET', `/profiles/${enc(id)}`),
	createProfile: (data) => request('POST', '/profiles', data),
	// data may also carry remote_pin ('12345678' | null) and remote_pin_fixed.
	updateProfile: (id, data, opts) => request('PATCH', `/profiles/${enc(id)}${dry(opts)}`, data),
	deleteProfile: (id, replacement, opts) =>
		request('DELETE', `/profiles/${enc(id)}${query({ replacement, dry_run: opts?.dryRun ? 'true' : null })}`),
	duplicateProfile: (id, name) => request('POST', `/profiles/${enc(id)}/duplicate`, name ? { name } : {}),
	applySuggestionUpdate: (id, opts) => request('POST', `/profiles/${enc(id)}/apply-suggestion${dry(opts)}`),
	profileFromDevice: (data) => request('POST', '/profiles/from-device', data),
	getProfileSuggestions: () => request('GET', '/profile-suggestions'),
	addProfileSuggestion: (id, name) => request('POST', `/profile-suggestions/${enc(id)}`, name ? { name } : {}),
	getProfileSettings: () => request('GET', '/settings/profiles'),
	saveProfileSettings: (data) => request('PUT', '/settings/profiles', data),
	checkProfile: (id, data) => request('POST', `/profiles/${enc(id)}/check`, data),
	assignClientProfile: (clientId, data, opts) => request('PUT', `/pairing/clients/${clientId}/profile${dry(opts)}`, data),
	getClientEffective: (clientId) => request('GET', `/pairing/clients/${clientId}/effective`),
	setClientOverride: (clientId, rule, data, opts) =>
		request('PUT', `/pairing/clients/${clientId}/overrides/${enc(rule)}${dry(opts)}`, data),
	removeClientOverride: (clientId, rule, opts) => request('DELETE', `/pairing/clients/${clientId}/overrides/${enc(rule)}${dry(opts)}`),
	setClientOwnArea: (clientId, areaId, opts) => request('PUT', `/pairing/clients/${clientId}/own-area${dry(opts)}`, { area_id: areaId }),
	// Remote PIN of the device (8 digits, null removes it); never shown again.
	setClientRemotePin: (clientId, pin) => request('PUT', `/pairing/clients/${clientId}/remote-pin`, { pin }),
	// History of the profile model with before/after (newest first): { client_id?, profile_id?, limit? }
	getProfileAudit: (params = {}) => request('GET', `/profile-audit${query(params)}`),
	// Several devices at once: { client_ids, action, …, dry_run } (clients-bulk.request).
	bulkClients: (data) => request('POST', '/pairing/clients/bulk', data),
	getKidsStatus: (clientId) => request('GET', `/pairing/clients/${clientId}/kids-status`),
	// minutes (15/30/60/120) or 'bedtime_end' ("until tomorrow morning" = end of the bedtime)
	startKidsOverride: (clientId, release) =>
		request('POST', `/pairing/clients/${clientId}/kids-override`, release === 'bedtime_end' ? { until: 'bedtime_end' } : { minutes: release }),
	endKidsOverride: (clientId) => request('DELETE', `/pairing/clients/${clientId}/kids-override`),
	setAccessSwitch: (clientId, on) => request('PUT', `/pairing/clients/${clientId}/access-switch`, { on }),
	// Early access: requests, the admin's answer, end an early access.
	listAccessRequests: () => request('GET', '/access-requests'),
	answerAccessRequest: (requestId, data) => request('POST', `/access-requests/${enc(requestId)}/answer`, data),
	endEarlyAccess: (clientId) => request('DELETE', `/pairing/clients/${clientId}/early-access`),
	// Family manager: children and HA notify targets per device, overview,
	// house settings, the admin answers a child's request (first answer wins).
	getClientPanel: (clientId) => request('GET', `/pairing/clients/${clientId}/panel`),
	sendPanelCommand: (clientId, data) => request('POST', `/pairing/clients/${clientId}/panel`, data),
	getClientFamily: (clientId) => request('GET', `/pairing/clients/${clientId}/family`),
	setClientFamily: (clientId, data) => request('PUT', `/pairing/clients/${clientId}/family`, data),
	getFamily: () => request('GET', '/family'),
	getHomeNetworks: () => request('GET', '/settings/home-networks'),
	// NFC/QR tags
	getScanTags: () => request('GET', '/scan-tags'),
	createScanTag: (data) => request('POST', '/scan-tags', data),
	updateScanTag: (tagId, data) => request('PUT', `/scan-tags/${enc(tagId)}`, data),
	deleteScanTag: (tagId) => request('DELETE', `/scan-tags/${enc(tagId)}`),
	saveHomeNetworks: (networks) => request('PUT', '/settings/home-networks', { networks }),
	getFamilySettings: () => request('GET', '/settings/family'),
	saveFamilySettings: (data) => request('PUT', '/settings/family', data),
	answerFamilyRequest: (requestId, data) => request('POST', `/family/requests/${enc(requestId)}/answer`, data),
	extendGuest: (clientId, data) => request('PATCH', `/clients/${clientId}/extend`, data),
	kickClient: (clientId) => request('POST', `/clients/${clientId}/kick`),
	// Remove an already-pending client from the list (hides it as a background tombstone).
	dismissPending: (clientId) => request('POST', `/clients/${clientId}/dismiss-pending`),

	// Global HA URL Settings
	getHaUrls: () => request('GET', '/settings/ha-urls'),
	saveHaUrls: (data) => request('PUT', '/settings/ha-urls', data),

	// New HA token for the device's own HA user (minted by the integration;
	// 200 delivered, 202 still running, 409 no token yet, 503 integration away)
	rotateClientToken: (clientId) => request('POST', `/clients/${clientId}/rotate-token`),

	// App URL Setting
	getAppUrl: () => request('GET', '/settings/app-url'),
	saveAppUrl: (app_url) => request('PUT', '/settings/app-url', { app_url }),

	// Default Control Mode
	getDefaultControlMode: () => request('GET', '/settings/control-mode'),
	saveDefaultControlMode: (data) => request('PUT', '/settings/control-mode', data),

	// Pairing via Integration (admin gate: may the companion integration start pairing?)
	getPairingViaIntegration: () => request('GET', '/settings/pairing-via-integration'),
	savePairingViaIntegration: (data) => request('PUT', '/settings/pairing-via-integration', data),

	// Device compliance status endpoint (removed/expired devices learn their status via the HA remote URL)
	getComplianceEndpoint: () => request('GET', '/settings/compliance-endpoint'),
	saveComplianceEndpoint: (data) => request('PUT', '/settings/compliance-endpoint', data),
	getAdminDirectAccess: () => request('GET', '/settings/admin-direct-access'),
	saveAdminDirectAccess: (data) => request('PUT', '/settings/admin-direct-access', data),

	// App Appearance / Home Screen
	getHomeSettings: () => request('GET', '/settings/home'),
	saveHomeSettings: (data) => request('PUT', '/settings/home', data),
	uploadHomeBgLight: (file) => upload('/settings/home/image/bg_light', file),
	uploadHomeBgDark: (file) => upload('/settings/home/image/bg_dark', file),
	deleteHomeBgLight: () => request('DELETE', '/settings/home/image/bg_light'),
	deleteHomeBgDark: () => request('DELETE', '/settings/home/image/bg_dark'),

	// Notifications
	getNotifications: (status = 'active') => request('GET', `/notifications?status=${status}`),
	getClientNotifications: (clientId, status = 'active') => request('GET', `/notifications/client/${clientId}?status=${status}`),
	// Curated set of suggested MDI icon names for the compose form.
	getNotificationIcons: () => request('GET', '/notifications/icons'),
	// Carries notification_type, popup, scheduled_at alongside title/message/icon/client_id.
	createNotification: (data) => request('POST', '/notifications', data),
	deleteNotification: (id) => request('DELETE', `/notifications/${id}`),
	clearClientNotifications: (clientId) => request('POST', `/notifications/client/${clientId}/clear`),

	// Health
	health: () => request('GET', '/health'),

	// System / Admin (admin-only, behind require_admin)
	adminLogs: (lines = 200) => request('GET', `/admin/logs?lines=${lines}`),
	adminRestart: () => request('POST', '/admin/restart'),
	// Companion integration connection telemetry: { connected, push_connected,
	// last_request_ts, request_count, auth_failure_count, integration_version,
	// addon_version }.
	integrationStatus: () => request('GET', '/admin/integration-status'),
	// Audit view: newest first since the hub started; categories
	// action | change | read | security.
	getAudit: (limit = 200, categories = []) => {
		const q = new URLSearchParams({ limit: String(limit) });
		for (const c of categories) q.append('category', c);
		return request('GET', `/admin/audit?${q}`);
	},
	// Log tab: persistent activity, changes and alarms.
	getLog: (params = {}) => request('GET', `/log${query(params)}`),
	getLogSummary: () => request('GET', '/log/summary'),
	exportLog: (params = {}) => request('GET', `/log/export${query(params)}`),
	markLogRead: (ids) => request('POST', '/log/read', ids ? { ids } : { all: true }),
	getLogSettings: () => request('GET', '/log/settings'),
	saveLogSettings: (data) => request('PUT', '/log/settings', data),
	getNotifyTargets: () => request('GET', '/log/notify-targets'),
	getEventNotifications: () => request('GET', '/settings/event-notifications'),
	saveEventNotifications: (data) => request('PUT', '/settings/event-notifications', data),
	testEventNotification: (body) => request('POST', '/settings/event-notifications/test', body),
	// Setup assistant progress: { status: new|in_progress|dismissed|completed,
	// step, skipped[] }; whether a step is done comes from the live data.
	getSetupAssistant: () => request('GET', '/admin/setup-assistant'),
	saveSetupAssistant: (data) => request('PUT', '/admin/setup-assistant', data),

	// Encryption / TLS (admin-only). getTlsSettings returns { tls_enabled,
	// tls_port, fingerprint, pending_fingerprint, cert }; regenerateTlsCert
	// rotates the self-signed cert and returns { fingerprint, restart_required,
	// message }.
	getTlsSettings: () => request('GET', '/settings/tls'),

	// API docs: where this admin UI opens them (under Ingress the Ingress
	// path, the docs are admin-only) and whether they are served at all
	// (add-on option api_docs; 404 when off).
	apiDocsUrl: (page = 'docs') => `${getBasePath()}/${page}`,
	apiDocsAvailable: async () => {
		try {
			const res = await fetch(`${getBasePath()}/docs`, { headers: getAuthHeaders('GET'), credentials: 'same-origin' });
			return res.ok;
		} catch {
			return false;
		}
	},
	regenerateTlsCert: () => request('POST', '/settings/tls/regenerate'),
};
