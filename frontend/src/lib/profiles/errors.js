/**
 * Readable texts for the error codes of the profile API (422/409
 * detail.code). Unknown codes keep the hub's message.
 */
import { t } from '$lib/i18n/index.svelte.js';
import { L, ruleOf } from './catalog.svelte.js';

const KNOWN = [
	'override.fixed',
	'override.boundary',
	'override.not_applicable',
	'override.value',
	'override.expires_at',
	'override.user_choice',
	'profile.limit',
	'profile.guest_child_protection',
	'name.duplicate',
	'profile_in_use',
	'profile_is_default',
	'last_profile',
	'no_suggestion_update',
	'remote_pin.fixed',
];

export function errorText(e) {
	const code = e?.code || '';
	if (KNOWN.includes(code)) return t(`profiles.err.${code.replace(/\./g, '_')}`);
	const m = /^rules\.(\w+)$/.exec(code);
	if (m) return t('profiles.err.rule', { rule: L(ruleOf(m[1])?.label) || m[1] });
	return e?.message || String(e);
}
