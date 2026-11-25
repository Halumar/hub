import { t } from '$lib/i18n/index.svelte.js';

/**
 * What a sync from Home Assistant left out (``result.errors`` of
 * POST /sync/from-ha and /sync/area/{id}/entities), as one short line:
 * count and the first three entries; null when nothing was left out.
 */
export function syncProblems(result) {
	const errors = Array.isArray(result?.errors) ? result.errors : [];
	if (errors.length === 0) return null;
	const list = errors.slice(0, 3).join(' · ') + (errors.length > 3 ? ` (+${errors.length - 3})` : '');
	return t('areas.importSkipped', { count: errors.length, list });
}
