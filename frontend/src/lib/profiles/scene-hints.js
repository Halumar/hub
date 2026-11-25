/**
 * Scenes and scripts whose targets a profile or device may not switch
 * (`scene_hints` of GET /profiles/{id}, the save preview and
 * GET /pairing/clients/{id}/effective; hub services/scene_targets.py).
 */

const ORDER = ['outside_areas', 'not_controllable', 'kids_blocked', 'remote'];

/** Lines of one hint: targets grouped by reason, in a fixed order. */
export function hintLines(hint) {
	const by = new Map();
	for (const target of hint?.targets || []) {
		if (!by.has(target.reason)) by.set(target.reason, []);
		by.get(target.reason).push(target);
	}
	return ORDER.filter((r) => by.has(r)).map((reason) => ({ reason, targets: by.get(reason) }));
}

/** Areas a target could be reached through (first area of each target, unique). */
export function areasToAdd(targets) {
	const out = [];
	for (const t of targets || []) {
		const a = t.area_ids?.[0];
		if (a && !out.includes(a)) out.push(a);
	}
	return out;
}

/** Whether an area belongs to an `areas` rule value (own area: not decidable, no). */
export function areaInRule(rule, areaId, tagsOf = () => []) {
	if (!rule || !areaId) return false;
	if (rule.mode === 'all') return true;
	if ((rule.area_ids || []).includes(areaId)) return true;
	const tags = tagsOf(areaId) || [];
	return (rule.tags || []).some((t) => tags.includes(t));
}

/**
 * Hints of the saved profile, adjusted to the unsaved `areas` value of the
 * editor: scenes no longer in the profile drop out, targets whose area was
 * added drop out. New refusals appear in the save preview (server side).
 */
export function hintsForDraft(hints, rule, tagsOf) {
	const out = [];
	for (const h of hints || []) {
		if (h.area_ids?.length && !h.area_ids.some((a) => areaInRule(rule, a, tagsOf))) continue;
		const targets = (h.targets || []).filter((t) => t.reason !== 'outside_areas' || !t.area_ids?.some((a) => areaInRule(rule, a, tagsOf)));
		if (targets.length || h.targets_unknown) out.push({ ...h, targets });
	}
	return out;
}
