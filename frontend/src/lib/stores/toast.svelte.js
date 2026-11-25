/**
 * Global Toast Notification Store (Svelte 5 runes)
 *
 * Replaces all alert() and inline error messages with a consistent
 * non-blocking notification system.
 *
 * Usage:
 *   import { toasts, addToast, dismissToast } from '$lib/stores/toast.svelte.js';
 *   addToast('Area created', 'success');
 *   addToast('Something failed', 'error', 8000);
 */

let _toasts = $state([]);
let _nextId = 0;

export const toasts = {
	get list() { return _toasts; },
};

/**
 * @param {string} message
 * @param {'info'|'success'|'warning'|'error'} type
 * @param {number} duration - ms before auto-dismiss (0 = manual only)
 */
export function addToast(message, type = 'info', duration = 4000) {
	const id = ++_nextId;
	_toasts = [..._toasts, { id, message, type, visible: true }];

	if (duration > 0) {
		setTimeout(() => dismissToast(id), duration);
	}
	// Keep max 6 visible toasts
	if (_toasts.length > 6) {
		_toasts = _toasts.slice(-6);
	}
	return id;
}

export function dismissToast(id) {
	_toasts = _toasts.filter(t => t.id !== id);
}

export function clearToasts() {
	_toasts = [];
}
