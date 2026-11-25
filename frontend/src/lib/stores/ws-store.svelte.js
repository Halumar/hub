/**
 * WebSocket Connection Store (Svelte 5 runes)
 *
 * Provides reactive connection state and auto-dispatches WS broadcasts
 * to update page data in real-time.
 *
 * Usage:
 *   import { wsConnection } from '$lib/stores/ws-store.svelte.js';
 *   wsConnection.status   // see STATUS below
 *   wsConnection.onBroadcast('area_updated', (data) => { ... })
 *
 * Status values:
 *   'idle'          socket not started (not logged in yet)
 *   'connecting'    socket opening / waiting for the server's auth answer
 *   'connected'     admin session authenticated: broadcasts arrive ("Live")
 *   'limited'       socket reachable, but the server did not authenticate
 *                   this session as admin: NO live updates
 *   'reconnecting'  connection lost, retrying
 *   'disconnected'  gave up retrying
 */

import { ws } from '$lib/ws.js';

// ── Reactive connection state ────────────────────────────────────────

let _status = $state('idle');
let _connectedAt = $state(null);
let _reconnectCount = $state(0);

export const wsConnection = {
	get status() { return _status; },
	get connectedAt() { return _connectedAt; },
	get reconnectCount() { return _reconnectCount; },
	get isConnected() { return _status === 'connected'; },
};

// ── Broadcast listener registry ──────────────────────────────────────
// Pages register callbacks for specific broadcast types.
// Returns an unsubscribe function.

/** @type {Map<string, Set<Function>>} */
const _broadcastListeners = new Map();

/**
 * Register a callback for a specific WS broadcast type.
 * The pseudo type 'resync' fires after the admin socket came back from a
 * disconnect: broadcasts in the gap were lost, so pages should reload quietly.
 * @param {string} type - e.g. 'area_updated', 'entity_added', 'resync'
 * @param {Function} callback - receives the broadcast data
 * @returns {Function} unsubscribe
 */
export function onBroadcast(type, callback) {
	if (!_broadcastListeners.has(type)) {
		_broadcastListeners.set(type, new Set());
	}
	_broadcastListeners.get(type).add(callback);
	return () => _broadcastListeners.get(type)?.delete(callback);
}

function emitBroadcast(type, data) {
	_broadcastListeners.get(type)?.forEach(cb => {
		try { cb(data); } catch (e) { console.error(`[WS Store] Broadcast listener error (${type}):`, e); }
	});
}

// ── Wire up the WS singleton ─────────────────────────────────────────

if (ws) {
	ws.on('connecting', () => {
		// Keep 'reconnecting' / 'limited' visible while a retry is in flight.
		if (_status === 'idle') _status = 'connecting';
	});

	ws.on('authenticated', ({ resumed }) => {
		_status = 'connected';
		_connectedAt = new Date();
		_reconnectCount = 0;
		if (resumed) emitBroadcast('resync', {});
	});

	ws.on('unauthenticated', () => {
		_status = 'limited';
		_connectedAt = null;
	});

	ws.on('disconnected', () => {
		_status = 'reconnecting';
		_reconnectCount++;
	});

	ws.on('gave_up', () => {
		_status = 'disconnected';
	});

	ws.on('stopped', () => {
		_status = 'idle';
		_connectedAt = null;
	});

	// All broadcast types from the backend
	const broadcastTypes = [
		'area_created',
		'area_updated',
		'area_deleted',
		'areas_deleted',
		'entity_added',
		'entity_updated',
		'entity_removed',
		'layout_updated',
		'section_created',
		'section_updated',
		'section_deleted',
		'widget_created',
		'widget_updated',
		'widget_deleted',
		'client_paired',
		'onboarding_started',
		'onboarding_claimed',
		'onboarding_ended',
		'guest_invite_opened',
		'guest_invite_closed',
		'client_connected',
		'client_disconnected',
		'client_kicked',
		'clients_cleared',
		'client_status_updated',
		'device_activity',
		'guest_extended',
		'guests_expired',
		'client_config_updated',
		'profiles_changed',
		'access_requests_changed',
		'notification_created',
		'notification_delivered',
		'notification_deleted',
		'notifications_cleared',
		'log_entry',
		'log_read',
	];

	for (const type of broadcastTypes) {
		ws.on(type, (data) => emitBroadcast(type, data));
	}
}
