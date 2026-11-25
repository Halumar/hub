// Client info of a device: platform, app version, activity feed.
import { t } from '$lib/i18n/index.svelte.js';
import { formatWeekdayTime } from '$lib/utils.js';
import { reasonLabel } from '$lib/data/kids.js';

// Android device classes use the icon of their Apple counterpart.
const PLATFORM_ICONS = {
	ios: 'iphone',
	ipados: 'ipad',
	tvos: 'tv',
	watchos: 'watch',
	android: 'iphone',
	android_tablet: 'ipad',
	android_tv: 'tv',
	wearos: 'watch',
	web: 'globe',
};

/** Device platforms in display order, each with its device class. */
export const DEVICE_PLATFORMS = [
	{ id: 'ios', cls: 'phone' },
	{ id: 'ipados', cls: 'tablet' },
	{ id: 'tvos', cls: 'tv' },
	{ id: 'watchos', cls: 'watch' },
	{ id: 'android', cls: 'phone' },
	{ id: 'android_tablet', cls: 'tablet' },
	{ id: 'android_tv', cls: 'tv' },
	{ id: 'wearos', cls: 'watch' },
];

/**
 * Platforms the protocol knows but no Halumar app exists for yet: the hub
 * accepts them and keeps stored defaults, the admin page lists them nowhere.
 */
export const NO_APP_PLATFORMS = new Set(['wearos', 'web']);

/** `ids` without the platforms that have no app (for every list shown to the admin). */
export function shownPlatforms(ids) {
	return (ids || []).filter((id) => !NO_APP_PLATFORMS.has(id));
}

/** DEVICE_PLATFORMS without the platforms that have no app. */
export const SHOWN_DEVICE_PLATFORMS = DEVICE_PLATFORMS.filter((p) => !NO_APP_PLATFORMS.has(p.id));

/** Icon of a platform (generic device shape; unknown → phone). */
export function platformIcon(platform) {
	return PLATFORM_ICONS[platform] || 'iphone';
}

/** A watch: no hub channel, it shows the notifications of the phone that set it up. */
export function isWatch(client) {
	return PLATFORM_ICONS[client?.platform] === 'watch';
}

/**
 * Readable list of platforms, e.g. the platforms of a profile rule: a device
 * class whose Apple and Android platforms are both listed is named once
 * ("Telefon"), single platforms by their device type ("iPad").
 */
export function platformsSummary(platforms) {
	const set = new Set(shownPlatforms(platforms));
	const out = [];
	const done = new Set();
	for (const p of SHOWN_DEVICE_PLATFORMS) {
		if (!set.has(p.id) || done.has(p.cls)) continue;
		const pair = SHOWN_DEVICE_PLATFORMS.filter((q) => q.cls === p.cls).map((q) => q.id);
		if (pair.every((id) => set.has(id))) {
			out.push(t(`profiles.classes.${p.cls}`));
			done.add(p.cls);
		} else {
			out.push(t(`profiles.slots.${p.id}`));
		}
	}
	for (const p of set) if (!SHOWN_DEVICE_PLATFORMS.some((q) => q.id === p)) out.push(t(`profiles.slots.${p}`));
	return out.join(', ');
}

/**
 * Default profiles grouped by profile: "iPhone, Apple Watch, Android-Telefon
 * → Erwachsene Person · iPad, Android-Tablet → Wandpanel". `names` maps a
 * profile id to its name; slots without a known profile and platforms
 * without an app are left out.
 */
export function defaultProfilesSummary(defaults, names, slots = [...SHOWN_DEVICE_PLATFORMS.map((p) => p.id), 'guest']) {
	const groups = new Map();
	for (const slot of shownPlatforms(slots)) {
		const pid = defaults?.[slot];
		if (!pid || !names[pid]) continue;
		if (!groups.has(pid)) groups.set(pid, []);
		groups.get(pid).push(t(`profiles.slots.${slot}`));
	}
	return [...groups].map(([pid, labels]) => `${labels.join(', ')} → ${names[pid]}`).join(' · ');
}

/** Icon of a device: from the reported platform, else guessed from the name. */
export function deviceIcon(client) {
	const icon = PLATFORM_ICONS[client?.platform];
	if (icon) return icon;
	const n = (client?.name || '').toLowerCase();
	if (n.includes('watch')) return 'watch';
	if (n.includes('tv')) return 'tv';
	if (n.includes('ipad') || n.includes('panel')) return 'ipad';
	return 'iphone';
}

/** "iPadOS", "tvOS" … or '' when the device has not reported it. */
export function platformLabel(platform) {
	return platform ? t(`devices.platforms.${platform}`) : '';
}

/** "2026.12.0 (6)", "2026.12.0" or '' when unknown. */
export function appVersionLabel(client) {
	if (!client?.app_version) return '';
	return client.app_build ? `${client.app_version} (${client.app_build})` : client.app_version;
}

/** Short app line for lists: "iOS · 2026.12.0" (or what is known). */
export function appSummary(client) {
	return [platformLabel(client?.platform), client?.app_version].filter(Boolean).join(' · ');
}

const ACTIVITY_ICONS = {
	paired: 'link',
	connected: 'wifi',
	disconnected: 'wifioff',
	app_updated: 'download',
	upgrade_required: 'warn',
	token_issued: 'key',
	token_rotated: 'refresh',
	device_added: 'tv',
	guest_invited: 'users',
	rule_blocked: 'lock',
	profile_changed: 'child',
	kids_override: 'clock',
};

export function activityIcon(event) {
	return ACTIVITY_ICONS[event] || 'history';
}

/** Readable line for one activity entry. */
export function activityLabel(entry) {
	const d = entry?.detail || {};
	switch (entry?.event) {
		case 'paired': {
			const app = [platformLabel(d.platform), d.app_version].filter(Boolean).join(' ') || '–';
			if (d.method === 'invite') return t('devices.activity.pairedByInvite', { app });
			return d.method === 'onboard' ? t('devices.activity.pairedByDevice', { app }) : t('devices.activity.paired', { app });
		}
		case 'device_added':
			return t('devices.activity.deviceAdded', { name: d.added_client_name || '–' });
		case 'guest_invited':
			return t('devices.activity.guestInvited', { name: d.guest_client_name || '–' });
		case 'connected':
			return d.app_version ? t('devices.activity.connectedWith', { version: d.app_version }) : t('devices.activity.connected');
		case 'app_updated':
			return t('devices.activity.appUpdated', { from: d.previous_app_version || '–', to: d.app_version || '–' });
		case 'upgrade_required':
			return d.reason === 'app_version'
				? t('devices.activity.upgradeApp', { version: d.app_version || '–' })
				: t('devices.activity.upgradeProtocol');
		case 'disconnected':
			return t('devices.activity.disconnected');
		case 'token_issued':
			return t('devices.activity.tokenIssued');
		case 'token_rotated':
			return t('devices.activity.tokenRotated');
		case 'rule_blocked': {
			const params = { entity: d.entity_id || '–', reason: reasonLabel(d.blocked_reason), time: d.until ? formatWeekdayTime(d.until) : '' };
			return d.until ? t('kids.activity.ruleBlockedUntil', params) : t('kids.activity.ruleBlocked', params);
		}
		case 'profile_changed':
			return d.profile_id ? t('kids.activity.profileChild', { name: d.profile_name || '–' }) : t('kids.activity.profileAdult');
		case 'kids_override':
			if (d.action !== 'release') return t('kids.activity.lift');
			return d.minutes ? t('kids.activity.release', { n: d.minutes }) : t('kids.activity.releaseUntil', { time: d.until ? formatWeekdayTime(d.until) : '–' });
		default:
			return entry?.event || '';
	}
}

/**
 * The hub reports that it cannot resolve the device's rules: `control_path`
 * null (or "unresolved"), or `rules_unresolved: true`. A missing field is not
 * this state; it only means the hub does not send the level.
 */
export function rulesUnresolved(client) {
	return client?.control_path === null || client?.control_path === 'unresolved' || client?.rules_unresolved === true;
}

/** Label of an access level, including "unknown". */
export function accessLabel(level) {
	if (level === 'unknown') return t('devices.levelUnknown');
	const key = `profiles.enum.control_path.${level}`;
	const s = t(key);
	return s === key ? String(level) : s;
}

/**
 * Home Assistant access of a device: the level ('hub' | 'checked' | 'direct',
 * the effective rule control_path; `path` from the effective rules wins over
 * the list field) and the key the device holds: 'none', 'read_only', 'full',
 * 'revoking' (on "Hub only" without remote access the device still knows a
 * key the hub has not rotated yet), or 'pending' while the integration still
 * sets up a device that will hold one. has_ha_token says whether the device
 * itself holds a key; null when the level is unknown.
 */
export function haAccessOf(client, path = null) {
	// The hub could not resolve the device's rules: level and key are unknown (never shown as "no key").
	if (rulesUnresolved(client)) return { level: 'unknown', key: 'unknown' };
	const level = path || client?.control_path || null;
	if (!level) return { level: null, key: null };
	let key;
	const holds = level !== 'hub' || !!client?.remote_allowed;
	if (client?.has_ha_token) key = !holds ? 'revoking' : level === 'direct' ? 'full' : 'read_only';
	else if (client?.ha_token_status === 'pending' && holds) key = 'pending';
	else key = 'none';
	return { level, key };
}

/** Remote access of a device for lists: on, off, or unknown (the hub sends null when it cannot resolve the rules). */
export function remoteState(client) {
	const v = client?.remote_allowed;
	if (v === null || rulesUnresolved(client)) return { id: 'unknown', icon: 'warn', label: t('devices.remoteUnknown') };
	return v ? { id: 'on', icon: 'globe', label: t('profiles.device.remoteOn') } : { id: 'off', icon: 'home', label: t('profiles.device.remoteOff') };
}
