/**
 * Lazy access to the MDI icon catalog.
 *
 * mdi-icons.js is ~2.7 MB of SVG paths. A static import would put it into
 * the chunk of every page with an icon picker (areas, area editor,
 * notifications). The dynamic import keeps it in its own chunk that is
 * fetched once, on demand, and shared by all pickers.
 */

/** @type {Promise<{ list: [string, string][], map: Map<string, string> }> | null} */
let pending = null;

export function loadMdiIcons() {
	if (!pending) {
		pending = import('./mdi-icons.js').then((m) => ({ list: m.default, map: new Map(m.default) }));
		pending.catch(() => { pending = null; });
	}
	return pending;
}
