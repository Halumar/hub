/**
 * Halumar WebSocket Client (admin UI)
 *
 * The admin UI only listens for broadcasts. The server pings every 30 s and
 * drops connections that do not answer with a pong, so this client answers
 * every `ping` with `pong`.
 *
 * The socket is NOT opened on import: the layout calls start() once the UI is
 * authenticated (Ingress immediately, external access after login) and stop()
 * on logout / session expiry.
 *
 * Authentication state (emitted as events):
 *   'authenticated'    server sent auth_ok -> admin broadcasts will arrive
 *   'unauthenticated'  server sent auth_required: the session is not an admin
 *                      WebSocket session (e.g. external cookie login on a
 *                      server without cookie WS auth). The socket is closed
 *                      right away instead of running into the 30 s auth
 *                      timeout, and retried slowly.
 *   'session_expired'  server reported an expired/invalid admin session
 *                      ({type:'session_expired'} or close code 4401).
 */

/** Close code a server uses to reject an expired/invalid admin session. */
export const WS_CLOSE_SESSION_EXPIRED = 4401;
/** Retry delay after the server refused to authenticate this session. */
const UNAUTHENTICATED_RETRY_MS = 5 * 60 * 1000;

export class HubConnection {
	/** @type {WebSocket|null} */
	ws = null;
	/** @type {Map<string, Set<Function>>} */
	listeners = new Map();
	reconnectTimer = null;
	reconnectDelay = 1000;
	maxReconnectDelay = 30000;
	reconnectAttempts = 0;
	maxReconnectAttempts = 50;
	/** True between start() and stop(). */
	running = false;
	/** 'pending' | 'admin' | 'unauthenticated' */
	authState = 'pending';
	/** Track consecutive fast closes (< 5s) to detect auth loops */
	_connectTime = 0;
	_fastCloseCount = 0;
	_hadAdminSession = false;

	start() {
		if (this.running) return;
		this.running = true;
		this.reconnectAttempts = 0;
		this.reconnectDelay = 1000;
		this.connect();
	}

	stop() {
		this.running = false;
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		const sock = this.ws;
		this.ws = null;
		this.authState = 'pending';
		if (sock) {
			sock.onclose = null;
			sock.close();
		}
		this.emit('stopped', {});
	}

	connect() {
		if (!this.running) return;
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const ingressPath = window.__INGRESS_PATH || '';
		const url = `${protocol}//${window.location.host}${ingressPath}/api/ws`;

		try {
			const sock = new WebSocket(url);
			this.ws = sock;
			this.authState = 'pending';
			this._connectTime = Date.now();
			this.emit('connecting', {});

			sock.onopen = () => {
				this.emit('connected', {});
			};

			sock.onmessage = (event) => {
				let msg;
				try {
					msg = JSON.parse(event.data);
				} catch (e) {
					console.error('[hub WS] Parse error:', e);
					return;
				}
				this.handleMessage(sock, msg);
			};

			sock.onclose = (event) => {
				if (this.ws !== sock) return;
				this.ws = null;
				const wasAdmin = this.authState === 'admin';
				const refused = this.authState === 'unauthenticated';
				this.authState = 'pending';

				if (event?.code === WS_CLOSE_SESSION_EXPIRED) {
					this.emit('session_expired', {});
					return;
				}
				if (!this.running) return;

				if (refused) {
					// Server will not authenticate this session; don't hammer it.
					this.scheduleReconnect(UNAUTHENTICATED_RETRY_MS);
					return;
				}

				// Detect connect loops: connection closed within 5s
				const lifespan = Date.now() - this._connectTime;
				this._fastCloseCount = lifespan < 5000 ? this._fastCloseCount + 1 : 0;
				if (this._fastCloseCount >= 5) {
					console.warn('[hub WS] Too many fast disconnects. Pausing reconnect for 60s.');
					this._fastCloseCount = 0;
					this.emit('disconnected', { wasAdmin });
					this.scheduleReconnect(60000);
					return;
				}

				this.emit('disconnected', { wasAdmin });
				this.scheduleReconnect();
			};

			sock.onerror = () => {
				// onclose follows and handles the reconnect.
			};
		} catch (e) {
			console.error('[hub WS] Connection failed:', e);
			this.scheduleReconnect();
		}
	}

	/** @param {WebSocket} sock @param {any} msg */
	handleMessage(sock, msg) {
		switch (msg?.type) {
			case 'ping':
				// Keep-alive: the server closes sockets that miss a pong.
				this.sendOn(sock, { type: 'pong' });
				return;
			case 'pong':
				return;
			case 'auth_ok': {
				this.authState = 'admin';
				this.reconnectDelay = 1000;
				this.reconnectAttempts = 0;
				const resumed = this._hadAdminSession;
				this._hadAdminSession = true;
				this.emit('authenticated', { client_id: msg.client_id, resumed });
				return;
			}
			case 'auth_required':
			case 'auth_timeout':
			case 'auth_error':
				this.authState = 'unauthenticated';
				this.emit('unauthenticated', { reason: msg.type, message: msg.message });
				// Close proactively instead of waiting for the 30 s auth timeout.
				try { sock.close(1000, 'admin session not authenticated'); } catch { /* already closed */ }
				return;
			case 'session_expired':
				this.authState = 'unauthenticated';
				this.emit('session_expired', {});
				return;
			default:
				this.emit(msg.type, msg.data || msg);
		}
	}

	scheduleReconnect(delay = this.reconnectDelay) {
		if (this.reconnectTimer || !this.running) return;
		if (this.reconnectAttempts >= this.maxReconnectAttempts) {
			console.error('[hub WS] Max reconnect attempts reached. Reload the page to retry.');
			this.emit('gave_up', {});
			return;
		}
		this.reconnectAttempts++;
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			this.connect();
			this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
		}, delay);
	}

	/** Reconnect now (e.g. tab became visible again or user clicked retry). */
	reconnectNow() {
		if (!this.running) return;
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		this.reconnectAttempts = 0;
		this.reconnectDelay = 1000;
		if (!this.ws) this.connect();
	}

	sendOn(sock, data) {
		if (sock?.readyState === WebSocket.OPEN) {
			sock.send(JSON.stringify(data));
		}
	}

	send(data) {
		this.sendOn(this.ws, data);
	}

	// ── Event System ────────────────────────────────────────────────────

	on(event, callback) {
		if (!this.listeners.has(event)) {
			this.listeners.set(event, new Set());
		}
		this.listeners.get(event).add(callback);
		return () => this.listeners.get(event)?.delete(callback);
	}

	emit(event, data) {
		this.listeners.get(event)?.forEach(cb => {
			try { cb(data); } catch (e) { console.error('[hub WS] Listener error:', e); }
		});
	}
}

/** Singleton WebSocket instance (connects only after start()). */
export const ws = typeof window !== 'undefined' ? new HubConnection() : null;
