/**
 * Does Home Assistant answer the apps unencrypted (http)? Source: the
 * internal URL of the settings (entered, else reported by the integration) and
 * an entered external URL. Returns the http URL or null.
 */
export function haCleartextUrl(haUrls) {
	if (!haUrls) return null;
	const candidates = [haUrls.ha_internal_url || haUrls.ha_internal_url_detected, haUrls.ha_url || haUrls.ha_url_detected];
	return candidates.find((u) => typeof u === 'string' && /^http:\/\//i.test(u.trim())) || null;
}

/** Help page on connecting securely (website). */
export function connectionDocsUrl(website) {
	return `${website.replace(/\/$/, '')}/docs/connection/`;
}
