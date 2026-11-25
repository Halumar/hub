/**
 * Filters of the log tab, shared by the entries page and the export in the
 * settings (kept while the admin switches between the log sections).
 */
export function freshFilters(mobile = false) {
	return { category: '', range: mobile ? '7d' : 'all', client_id: '', owner: '', profile_id: '', area_id: '', q: '' };
}

export const logState = $state({ filters: freshFilters(), mobileDefaultApplied: false });

export function queryOf(f, extra = {}) {
	return { ...f, range: f.range === 'all' ? '' : f.range, ...extra };
}

export function activeFilterCount(f, mobile = false) {
	const base = freshFilters(mobile);
	return ['category', 'range', 'client_id', 'owner', 'profile_id', 'area_id', 'q'].filter((k) => (f[k] || '') !== (base[k] || '')).length;
}
