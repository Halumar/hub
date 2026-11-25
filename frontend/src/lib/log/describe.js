/**
 * Texts of log entries for the timeline, the detail panel and the CSV
 * export. Entries come from GET /api/log (services/event_log.py view()).
 */
import { t, i18n } from '$lib/i18n/index.svelte.js';
import { BRAND } from '$lib/brand.ts';
import { formatWeekdayTime } from '$lib/utils.js';


/** Technical namespace (from GET /api/log/summary), e.g. for <ns>.control. */
let nsName = '';
export function setNamespace(ns) {
	nsName = ns || '';
}

export const KIND_ICON = { activity: 'power', change: 'edit', alarm: 'warn' };
export const KIND_CHIP = { activity: 'c-info', change: 'c-violet', alarm: 'c-warn' };
const ALARM_ICON = {
	denied: 'lock',
	access_group_restored: 'shield',
	direct_access_attempt: 'shield',
	integration_outage: 'wifioff',
};

export function locale() {
	return i18n.locale === 'en' ? 'en-GB' : 'de-CH';
}

function has(key) {
	return t(key) !== key;
}

export function categoryLabel(c) {
	return c === 'activity' ? t('log.activity') : c === 'change' ? t('log.change') : t('log.alarm');
}

export function alarmType(e) {
	return has(`log.alarmTypes.${e.kind}`) ? t(`log.alarmTypes.${e.kind}`) : '';
}

export function iconOf(e) {
	if (e.category === 'alarm') return ALARM_ICON[e.kind] || 'warn';
	return KIND_ICON[e.category] || 'history';
}

export function reasonText(code) {
	if (!code) return '';
	return has(`log.reasons.${code}`) ? t(`log.reasons.${code}`) : code;
}

export function entityLabel(e) {
	return e.entity_name || e.entity_id || '';
}

/** Who did it: device (person) for activity, the admin/automatic for changes. */
export function whoLabel(e) {
	if (e.category === 'change' && e.actor) {
		if (e.actor === BRAND.name) return t('log.automatic');
		if (e.source === 'hub' && !e.client_id) return t('log.adminSuffix', { name: e.actor });
	}
	if (e.source === 'integration' && !e.client_id) return t('log.integration');
	if (e.device_name) {
		return e.owner_label && !e.device_name.includes(e.owner_label) ? `${e.device_name} (${e.owner_label})` : e.device_name;
	}
	if (e.actor) return t('log.adminSuffix', { name: e.actor });
	if (e.kind === 'integration_outage') return t('log.integration');
	return BRAND.name;
}

function quoted(s) {
	return i18n.locale === 'en' ? `“${s}”` : `„${s}“`;
}

function actionText(e) {
	return (e.action || '').replace(/_/g, ' ');
}

/** What happened (without who). */
const FAMILY_KINDS = ['kids_release_requested', 'kids_request_answered', 'kids_request_expired', 'kids_request_cancelled', 'family_release', 'family_release_end'];

export function whatLabel(e) {
	const k = e.kind;
	if (['control', 'kids_control', 'denied', 'kids_bedtime_attempt', 'kids_pause', 'kids_paused_all'].includes(k) && e.category === 'activity') {
		const what = `${entityLabel(e)} ${actionText(e)}`.trim();
		if (k === 'kids_bedtime_attempt' || k === 'kids_pause' || k === 'kids_paused_all') {
			// One entry per pause; further attempts during it are counted.
			const more = (k === 'kids_pause' || k === 'kids_paused_all') && e.count > 1 ? ` (${e.count}×)` : '';
			return `${t(`log.kinds.${k}`)} · ${what}${more}`;
		}
		return what || t(`log.kinds.${k}`);
	}
	// Wall panel: device = the panel, actor = HA admin, automation or admin UI.
	if (k === 'panel_command' || k === 'panel_command_rejected') {
		const action = e.action && has(`devices.panel.action.${e.action}`) ? t(`devices.panel.action.${e.action}`) : actionText(e);
		let text = `${t(`log.kinds.${k}`)} · ${action}`;
		const code = e.detail?.error_code;
		if (code && has(`devices.panel.error.${code}`)) text += ` · ${t(`devices.panel.error.${code}`)}`;
		if (e.actor) text += ` · ${t('log.kinds.familyBy', { name: e.actor })}`;
		return text;
	}
	// NFC/QR tags: name and result; the app switches afterwards like a widget.
	if (k === 'tag_scanned') {
		let text = t('log.kinds.tag_scanned');
		if (e.detail?.tag_name) text += ` ${quoted(e.detail.tag_name)}`;
		const result = e.detail?.tag_result;
		if (result && has(`log.kinds.tagResult.${result}`)) text += ` · ${t(`log.kinds.tagResult.${result}`)}`;
		return text;
	}
	if (k === 'scan_tag_set') {
		const change = e.detail?.change;
		const name = e.detail?.after || e.detail?.before;
		let text = t('log.kinds.scan_tag_set');
		if (name) text += ` ${quoted(name)}`;
		if (change && has(`log.kinds.scanTagChange.${change}`)) text += ` · ${t(`log.kinds.scanTagChange.${change}`)}`;
		return text;
	}
	if (e.category === 'alarm' && k === 'denied') {
		return t('log.kinds.alarm_denied', { count: e.count || 1, minutes: e.detail?.window_minutes || 10 });
	}
	if (k === 'access_opened' || k === 'access_closed') {
		// Why and until when.
		let text = t(`log.kinds.${k}`);
		const reason = e.detail?.reason;
		if (reason) text += ` · ${t(reason === 'switch' ? 'log.kinds.accessBySwitch' : 'log.kinds.accessBySchedule')}`;
		if (k === 'access_opened' && e.detail?.until) text += ` · ${t('log.kinds.accessUntil', { time: formatWeekdayTime(e.detail.until) })}`;
		if (k === 'access_closed' && e.detail?.opens_at) text += ` · ${t('log.kinds.accessFrom', { time: formatWeekdayTime(e.detail.opens_at) })}`;
		if (k === 'access_opened' && e.detail?.early?.by) text += ` · ${t('log.kinds.accessEarly', { name: e.detail.early.by })}`;
		return text;
	}
	// Early access requests – the answer, until when, by whom.
	if (k === 'access_request_answered') {
		let text = t('log.kinds.access_request_answered');
		if (e.action === 'yes') text += ` · ${t('log.kinds.accessAnswerYes', { time: formatWeekdayTime(e.until) })}`;
		else if (e.action === 'no') text += ` · ${t('log.kinds.accessAnswerNo')}`;
		if (e.actor) text += ` · ${t('log.kinds.accessBy', { name: e.actor })}`;
		return text;
	}
	if (k === 'access_requested' && e.detail?.note) return `${t('log.kinds.access_requested')} ${quoted(e.detail.note)}`;
	if (k === 'device_set_up' && e.actor) {
		return t('log.kinds.device_set_up_by', { by: e.actor, profile: e.profile_name || '' });
	}
	if (k === 'direct_access_attempt' && e.count) return `${t('log.kinds.direct_access_attempt')} (${e.count}×)`;
	let text = has(`log.kinds.${k}`) ? t(`log.kinds.${k}`) : k;
	if ((k === 'device_paired' || k === 'guest_paired') && e.detail?.camera_qr) text += ` · ${t('log.kinds.viaCameraQr')}`;
	if ((k === 'profile_updated' || k === 'profile_created' || k === 'profile_deleted') && e.profile_name) text += ` ${quoted(e.profile_name)}`;
	if (k === 'profile_assigned' && e.profile_name) text += ` · ${e.profile_name}`;
	if ((k === 'override_set' || k === 'override_removed' || k === 'override_expired') && e.detail?.rule) text += ` · ${e.detail.rule}`;
	if (k === 'home_networks_set' && Array.isArray(e.detail?.after)) text += ` · ${e.detail.after.join(', ') || '–'}`;
	// A watch removed because its phone was removed (detail.how with_phone).
	if (k === 'device_removed' && e.detail?.with_device?.name) text += ` · ${t('log.kinds.withDevice', { name: e.detail.with_device.name })}`;
	// Family manager: what was asked / answered / released, by whom, until when.
	if (FAMILY_KINDS.includes(k)) {
		const kind = e.detail?.category || (k.startsWith('family_release') ? e.action : null);
		if (kind && has(`family.category.${kind}`)) text += ` · ${t(`family.category.${kind}`)}`;
		else if (kind === 'bedtime') text += ` · ${t('family.category.stay_up')}`;
		else if (kind === 'pause') text += ` · ${t('family.category.end_pause')}`;
		if (k === 'kids_request_answered' && e.action) text += ` · ${t(`family.status.${e.action === 'yes' ? 'approved' : 'declined'}`)}`;
		if (e.until && (k === 'kids_request_answered' || k === 'family_release')) text += ` · ${t('log.kinds.familyUntil', { time: formatWeekdayTime(e.until) })}`;
		if (e.detail?.by?.name && e.detail.by.kind === 'family_manager') text += ` · ${t('log.kinds.familyBy', { name: e.detail.by.name })}`;
		if (e.detail?.by?.name && e.detail.by.kind === 'babysitter') text += ` · ${t('log.kinds.babysitterBy', { name: e.detail.by.name })}`;
	}
	return text;
}

export function whereKey(e) {
	if (e.category !== 'activity' || !e.route) return null;
	return e.route === 'local' ? 'home' : 'away';
}

/** One line: who · what · where (activity). */
export function lineParts(e) {
	const parts = [whoLabel(e), whatLabel(e)];
	const w = whereKey(e);
	if (w) parts.push(w === 'away' ? t('log.awayLower') : t('log.atHomeLower'));
	return parts;
}

export function viaLabel(e) {
	if (e.control_path === 'direct') return t('log.via.ha');
	if (e.control_path === 'hub') return t('log.via.app');
	if (e.origin === 'quick_access') return t('log.via.quick_access');
	if (e.origin === 'app') return t('log.via.app');
	return '';
}

export function pathLabel(e) {
	if (!e.control_path) return '';
	return t(`log.path.${e.control_path}`, { ns: nsName });
}

export function outageMinutes(e) {
	if (!e.since) return null;
	const end = e.until ? Date.parse(e.until) : Date.parse(e.ts);
	return Math.max(1, Math.round((end - Date.parse(e.since)) / 60000));
}

// ── dates ─────────────────────────────────────────────────────────────────

export function dayKey(iso) {
	const d = new Date(iso);
	return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function dayLabel(key, now = new Date()) {
	const [y, m, d] = key.split('-').map(Number);
	const date = new Date(y, m - 1, d);
	const s = date.toLocaleDateString(locale(), { weekday: 'long', day: 'numeric', month: 'long' });
	const today = dayKey(now.toISOString());
	const yest = new Date(now);
	yest.setDate(yest.getDate() - 1);
	if (key === today) return `${t('log.today')} · ${s}`;
	if (key === dayKey(yest.toISOString())) return `${t('log.yesterday')} · ${s}`;
	return s;
}

export function timeLabel(iso) {
	return new Date(iso).toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
}

export function dateTimeLabel(iso) {
	return new Date(iso).toLocaleString(locale(), { weekday: 'short', day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
}

export function relLabel(iso, now = Date.now()) {
	const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: 'auto' });
	const diff = (Date.parse(iso) - now) / 1000;
	const abs = Math.abs(diff);
	if (abs < 60) return rtf.format(Math.round(diff), 'second');
	if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute');
	if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
	return rtf.format(Math.round(diff / 86400), 'day');
}

// ── CSV ───────────────────────────────────────────────────────────────────

export function csv(entries, anon = false) {
	const q = (s) => `"${String(s ?? '').replace(/"/g, '""')}"`;
	const head = t('log.csvHead').split(';');
	const rows = entries.map((e) => {
		const d = new Date(e.ts);
		const w = whereKey(e);
		return [
			dayKey(e.ts),
			d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
			categoryLabel(e.category),
			e.category === 'alarm' ? alarmType(e) : '',
			anon ? '' : e.owner_label || (e.category === 'change' ? e.actor || '' : ''),
			e.device_name || '',
			e.profile_name || '',
			e.area_name || '',
			w ? (w === 'away' ? t('log.awayLower') : t('log.atHomeLower')) : '',
			whatLabel(e),
			e.result === 'denied' ? t('log.notDone') : e.result === 'ok' ? t('log.done') : '',
			reasonText(e.reason),
		];
	});
	return '\ufeff' + [head, ...rows].map((r) => r.map(q).join(';')).join('\r\n');
}
