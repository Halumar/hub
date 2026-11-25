/**
 * Shared MDI name -> svg-path lookup.
 *
 * Builds the Map once at module load (module scope is shared across importers),
 * so components can render the actual icon for any "mdi:<name>" value without
 * re-scanning the ~7400-entry array on every render.
 */
import mdiIcons from './mdi-icons.js';

const map = new Map(mdiIcons);

/**
 * Resolve an svg path "d" for an MDI icon value.
 * Accepts "mdi:bell" or a bare "bell". Returns null for unknown / non-MDI values
 * (e.g. "sf:bell.fill"), so callers can fall back to a text/emoji representation.
 * @param {string} value
 * @returns {string|null}
 */
export function mdiPath(value) {
	if (!value) return null;
	const name = value.startsWith('mdi:') ? value.slice(4) : value;
	return map.get(name) || null;
}
