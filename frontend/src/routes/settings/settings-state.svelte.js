/**
 * State and loaders of the settings page, shared by its tabs. Everything is
 * loaded when the page opens, so the page-level error banner knows about
 * every setting that could not be loaded (its value is then unknown and the
 * control stays disabled instead of showing a default).
 */
import { api } from '$lib/api.js';
import { addToast } from '$lib/stores/toast.svelte.js';
import { t } from '$lib/i18n/index.svelte.js';

export function isValidUrl(v) {
	if (!v) return true;
	try {
		const u = new URL(v);
		return u.protocol === 'http:' || u.protocol === 'https:';
	} catch {
		return false;
	}
}

class SettingsState {
	loadErrors = $state({});
	loading = $state({});

	clients = $state([]);
	haUrls = $state({ ha_url: '', ha_internal_url: '', ha_url_detected: '', ha_internal_url_detected: '' });
	haUrlEdit = $state({ ha_url: '', ha_internal_url: '' });
	appUrl = $state('');
	homeNetworks = $state([]);
	appUrlEdit = $state('');
	home = $state({ home_title: '', home_bg_light: null, home_bg_dark: null, location_name: '' });
	homeTitleEdit = $state('');
	controlMode = $state('full');
	pairingViaIntegration = $state(true);
	compliance = $state(true);
	adminDirectAccess = $state(false);
	hasApiToken = $state(false);
	hasIntegrationToken = $state(false);
	integrationSupervisorManaged = $state(false);

	#ok(key) {
		if (key in this.loadErrors) {
			const { [key]: _drop, ...rest } = this.loadErrors;
			this.loadErrors = rest;
		}
	}
	#fail(key, e) {
		this.loadErrors = { ...this.loadErrors, [key]: e?.message || 'unknown error' };
	}
	async #load(key, fn) {
		this.loading = { ...this.loading, [key]: true };
		try {
			await fn();
			this.#ok(key);
		} catch (e) {
			this.#fail(key, e);
		} finally {
			this.loading = { ...this.loading, [key]: false };
		}
	}

	loaders = {
		clients: () =>
			this.#load('clients', async () => {
				this.clients = await api.getPairedClients();
			}),
		haUrls: () =>
			this.#load('haUrls', async () => {
				const r = await api.getHaUrls();
				this.haUrls = r;
				this.haUrlEdit = { ha_url: r.ha_url || '', ha_internal_url: r.ha_internal_url || '' };
			}),
		appUrl: () =>
			this.#load('appUrl', async () => {
				const r = await api.getAppUrl();
				this.appUrl = r.app_url || '';
				this.appUrlEdit = this.appUrl;
			}),
		homeNetworks: () =>
			this.#load('homeNetworks', async () => {
				const r = await api.getHomeNetworks();
				this.homeNetworks = Array.isArray(r?.networks) ? r.networks : [];
			}),
		home: () =>
			this.#load('home', async () => {
				const r = await api.getHomeSettings();
				this.home = r;
				this.homeTitleEdit = r.home_title || '';
			}),
		controlMode: () =>
			this.#load('controlMode', async () => {
				const r = await api.getDefaultControlMode();
				this.controlMode = r.control_mode || 'full';
			}),
		pairingViaIntegration: () =>
			this.#load('pairingViaIntegration', async () => {
				const r = await api.getPairingViaIntegration();
				this.pairingViaIntegration = r.allow_pairing_via_integration !== false;
			}),
		compliance: () =>
			this.#load('compliance', async () => {
				const r = await api.getComplianceEndpoint();
				this.compliance = r.compliance_endpoint_enabled !== false;
			}),
		adminDirectAccess: () =>
			this.#load('adminDirectAccess', async () => {
				const r = await api.getAdminDirectAccess();
				this.adminDirectAccess = r.admin_direct_access === true;
			}),
		apiToken: () =>
			this.#load('apiToken', async () => {
				const r = await api.checkApiToken();
				this.hasApiToken = r.has_token === true;
			}),
		integrationToken: () =>
			this.#load('integrationToken', async () => {
				const r = await api.checkIntegrationToken();
				this.hasIntegrationToken = r.has_token === true;
				this.integrationSupervisorManaged = r.supervisor_managed === true;
			}),
	};

	loadAll() {
		return Promise.all(Object.values(this.loaders).map((fn) => fn()));
	}

	retryFailed() {
		for (const key of Object.keys(this.loadErrors)) this.loaders[key]?.();
	}

	/** Run a save; toast on error, return the result or null. */
	async save(fn, okMessage, failKey) {
		try {
			const r = await fn();
			if (okMessage) addToast(okMessage, 'success');
			return r ?? true;
		} catch (e) {
			addToast(t(failKey, { message: e.message }), 'error');
			return null;
		}
	}
}

export const settings = new SettingsState();
