// Home Assistant context under Ingress. The admin UI runs in an iframe on the
// HA origin (/api/hassio_ingress/<token>/), so the parent document – the HA
// frontend – is same-origin and exposes `hass` on <home-assistant>. Outside
// Ingress (or if HA changes its internals) this returns null and the UI falls
// back to the browser settings.

/** @returns {{ language: string|null, darkMode: boolean|null } | null} */
export function readHass() {
	if (typeof window === 'undefined' || !window.__INGRESS_PATH) return null;
	try {
		if (window.parent === window) return null;
		const hass = window.parent.document.querySelector('home-assistant')?.hass;
		if (!hass) return null;
		const language = hass.locale?.language || hass.language || null;
		const darkMode = typeof hass.themes?.darkMode === 'boolean' ? hass.themes.darkMode : null;
		return { language, darkMode };
	} catch {
		return null; // cross-origin or no HA frontend
	}
}

/**
 * Call `cb` when HA's theme or language may have changed: HA re-applies its
 * theme variables on the parent <html> (style attribute) and changes <html lang>.
 * @param {() => void} cb
 * @returns {() => void} unsubscribe
 */
export function watchHass(cb) {
	if (typeof window === 'undefined' || !window.__INGRESS_PATH) return () => {};
	try {
		// Not in an iframe: observing our own <html> would loop on our own lang changes.
		if (window.parent === window) return () => {};
		const target = window.parent.document.documentElement;
		const obs = new window.parent.MutationObserver(() => cb());
		obs.observe(target, { attributes: true, attributeFilter: ['style', 'class', 'lang'] });
		return () => obs.disconnect();
	} catch {
		return () => {};
	}
}
