/**
 * Reference data the rule editors need: areas (names for ids), area tags and
 * Home Assistant entities ("When away" per device, kids exceptions).
 * Each part fails soft (an empty list only hides choices).
 */
import { api } from '$lib/api.js';

/** @returns {Promise<{ areas: { id: string, name: string, tags: string[] }[], tags: string[], entities: any[] }>} */
export async function loadRuleContext({ entities = true } = {}) {
	const [a, tg, en] = await Promise.allSettled([
		api.getAreas(),
		api.getAvailableTags(),
		entities ? api.getHAEntities() : Promise.resolve([]),
	]);
	const areas = a.status === 'fulfilled' && Array.isArray(a.value) ? a.value.map((x) => ({ id: x.id, name: x.name, tags: Array.isArray(x.tags) ? x.tags : [] })) : [];
	const tags = tg.status === 'fulfilled' ? (tg.value?.tags || []).filter((x) => x !== '*' && !String(x).startsWith('area:')) : [];
	const raw = en.status === 'fulfilled' ? en.value : [];
	const list = Array.isArray(raw) ? raw : Array.isArray(raw?.entities) ? raw.entities : [];
	return {
		areas,
		tags,
		entities: list.map((e) => ({
			entity_id: e.entity_id,
			friendly_name: e.friendly_name || e.attributes?.friendly_name || e.name || e.entity_id,
			device_class: e.device_class || e.attributes?.device_class || null,
		})),
	};
}

/** tagsOf(id): tags of an area (scene hints of a draft). */
export function areaTagger(areas) {
	const map = new Map((areas || []).map((a) => [a.id, a.tags || []]));
	return (id) => map.get(id) || [];
}

/** areaName(id) for formatValue. */
export function areaNamer(areas) {
	const map = new Map((areas || []).map((a) => [a.id, a.name]));
	return (id) => map.get(id) || id;
}

/** Kids rules of a profile body in the shape of the kids rule editor (inherited values). */
export function kidsRulesOf(rules) {
	if (!rules) return null;
	return {
		blocked: false,
		blocked_types: rules.kids_blocked_types ?? [],
		quiet_hours: rules.kids_quiet_hours ?? [],
		rate_limit: rules.kids_rate_limit ?? null,
		color_lock: rules.kids_color_lock ?? false,
		volume_max: rules.kids_volume_max ?? null,
	};
}
