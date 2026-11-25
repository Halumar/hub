/**
 * Demo data for the screenshot tour and the tile tests (a German demo
 * home). Times are relative to "now" so
 * expiry bars and "last seen" texts look plausible on every run.
 */
const H = 3600_000;
const iso = (offsetMs) => new Date(Date.now() + offsetMs).toISOString();

export const demoAreas = [
	{ id: 'a-wohnzimmer', name: 'Wohnzimmer', icon: 'mdi:sofa', description: 'Erdgeschoss', color: null, grid_cols: 2, is_active: true, sort_order: 0, tags: ['eg', 'gast'], entity_count: 8, widget_count: 1, control_mode: null },
	{ id: 'a-kueche', name: 'Küche', icon: 'mdi:fridge', description: '', color: null, grid_cols: 2, is_active: true, sort_order: 1, tags: ['eg', 'gast'], entity_count: 5, control_mode: 'basic' },
	{ id: 'a-buero', name: 'Büro', icon: 'mdi:desk', description: '', color: null, grid_cols: 2, is_active: true, sort_order: 2, tags: ['og'], entity_count: 4, control_mode: 'full' },
	{ id: 'a-schlafzimmer', name: 'Schlafzimmer', icon: 'mdi:bed', description: '', color: null, grid_cols: 2, is_active: true, sort_order: 3, tags: ['og'], entity_count: 3, control_mode: null },
	{ id: 'a-eingang', name: 'Eingang', icon: 'mdi:door', description: '', color: null, grid_cols: 2, is_active: true, sort_order: 4, tags: ['eg'], entity_count: 4, control_mode: null },
	{ id: 'a-garten', name: 'Garten', icon: 'mdi:flower', description: '', color: null, grid_cols: 3, is_active: true, sort_order: 5, tags: ['aussen', 'gast'], entity_count: 3, control_mode: null },
	{ id: 'a-garage', name: 'Garage', icon: 'mdi:garage', description: 'Tor und Licht', color: null, grid_cols: 2, is_active: false, sort_order: 6, tags: ['aussen'], entity_count: 3, control_mode: 'advanced' },
];

const ent = (entity_id, display_name, sort_order, extra = {}) => ({
	entity_id, display_name, columns: 1, rows: 1, is_visible: true, control_mode: null, sort_order, ...extra,
});

export function demoAreaDetail(id = 'a-wohnzimmer') {
	const area = demoAreas.find((a) => a.id === id) || demoAreas[0];
	return {
		...area,
		sections: [
			{
				id: 's-licht', title: 'Licht & Klima', columns: 2, column_span: 1, sort_order: 0,
				entities: [
					ent('light.ceiling_lights', 'Deckenlicht', 0),
					ent('light.living_room_rgbww', 'Stehlampe', 1),
					ent('climate.heatpump', 'Klima Wohnzimmer', 2),
					ent('cover.living_room_window', 'Store Süd', 3),
				],
				widgets: [],
			},
			{
				id: 's-unterhaltung', title: 'Unterhaltung', columns: 2, column_span: 1, sort_order: 1,
				entities: [ent('media_player.living_room', 'Sonos Wohnzimmer', 0, { columns: 2 })],
				widgets: [],
			},
			{
				id: 's-info', title: 'Hausinfos', columns: 2, column_span: 1, sort_order: 2,
				entities: [ent('sensor.outside_temperature', 'Aussentemperatur', 0), ent('lock.front_door', 'Haustür', 1, { is_visible: false })],
				widgets: [{ id: 'w-1', widget_type: 'text', config: { text: '**WLAN:** Gäste-Netz, Passwort am Kühlschrank' }, columns: 2, rows: 1, sort_order: 2 }],
			},
		],
	};
}

const client = (o) => ({
	is_active: true, is_connected: true, is_guest: false, tags: [], created_at: iso(-30 * 24 * H), last_seen: iso(-60_000),
	has_ha_token: true, ha_token_status: 'active', ha_token_pending_since: null, owner_label: null, remote_allowed: false,
	expires_at: null, pending_deletion: false, notification_stacked_style: null, app_config_overrides: {}, config_template_id: null,
	settings_hidden: false, settings_lock_enabled: false, settings_require_biometrics: false, settings_pin_set: false,
	platform: 'ios', app_version: '2026.12.0', app_build: '6', device_model: 'iPhone18,1', protocol_version: 1, update_required: false, ...o,
});

export const demoClients = [
	client({ id: 'c-parent-iphone', name: 'Parent iPhone', owner_label: 'Owner A', remote_allowed: true, ha_user_id: 'u-parent-iphone', config_template_id: 't-familie', app_config_overrides: { card_style: 'color', card_style_locked: true } }),
	client({ id: 'c-parent-ipad', name: 'Parent iPad', owner_label: 'Owner A', platform: 'ipados', device_model: 'iPad16,3', ha_user_id: 'u-parent-ipad', last_seen: iso(-5 * 60_000) }),
	client({ id: 'c-parent2', name: 'Second parent iPhone', owner_label: 'Owner B', remote_allowed: true, is_connected: false, ha_user_id: 'u-parent2', last_seen: iso(-20 * H) }),
	client({ id: 'c-owner-b', name: 'Owner B iPhone', owner_label: 'Owner B', is_guest: true, tags: ['gast'], ha_user_id: 'u-owner-b', expires_at: iso(2 * 24 * H), created_at: iso(-24 * H) }),
	client({ id: 'c-tv', name: 'Living room TV', platform: 'tvos', device_model: 'AppleTV14,1', app_version: '2026.11.0', app_build: '5', update_required: true, ha_user_id: 'u-tv', config_template_id: 't-panel' }),
	client({ id: 'c-panel', name: 'Wandpanel Küche', platform: 'ipados', device_model: 'iPad13,18', ha_user_id: null, has_ha_token: false, ha_token_status: 'pending', ha_token_pending_since: iso(-30_000), created_at: iso(-60_000) }),
];

/** Activity feed of a device (GET /pairing/clients/{id}/activity). */
export function demoActivity(clientId) {
	const e = (id, ms, event, detail = null) => ({ id, client_id: clientId, ts: iso(ms), event, detail });
	return {
		client_id: clientId,
		max_events: 100,
		events: [
			e(6, -60_000, 'connected', { app_version: '2026.12.0', app_build: '6', protocol_version: 1 }),
			e(5, -60_500, 'app_updated', { previous_app_version: '2026.11.0', app_version: '2026.12.0' }),
			e(4, -3 * H, 'upgrade_required', { reason: 'app_version', app_version: '2026.11.0', protocol_version: 1 }),
			e(3, -20 * H, 'disconnected'),
			e(2, -29 * 24 * H, 'token_issued'),
			e(1, -30 * 24 * H, 'paired', { platform: 'ios', app_version: '2026.11.0' }),
		],
	};
}

export const demoTemplates = [
	{ id: 't-familie', name: 'Familie', description: 'Standard für Familienmitglieder', platform: 'ios', settings: { show_search_bar: true, area_card_columns: 2 } },
	{ id: 't-panel', name: 'Wandpanel', description: 'Gesperrt, immer wach', platform: null, settings: { settings_lock_enabled: true, settings_hidden: true } },
];

export const demoNotifications = [
	{ id: 'n-1', client_id: 'c-parent-iphone', client_name: 'Parent iPhone', title: 'Garagentor offen', message: 'Das Garagentor ist seit 20 Minuten offen.', icon: 'mdi:garage-open', notification_type: 'warning', popup_mode: 'default', status: 'delivered', created_at: iso(-2 * H), delivered_at: iso(-2 * H + 2000), connected: true, priority: 'normal' },
	{ id: 'n-2', client_id: 'c-panel', client_name: 'Wandpanel Küche', title: 'Waschmaschine fertig', message: 'Bitte ausräumen.', icon: 'mdi:washing-machine', notification_type: 'info', popup_mode: 'default', status: 'pending', created_at: iso(-10 * 60_000), connected: false, priority: 'normal' },
];

export const demoLogs = [
	'2026-11-18 10:00:01 INFO hub.main: Hub 2026.12.0-dev started (TLS on :8100)',
	'2026-11-18 10:00:02 INFO hub.ws: integration connected (protocol v1)',
	'2026-11-18 10:02:13 INFO hub.pairing: device "Parent iPhone" connected',
	'2026-11-18 10:04:40 WARNING hub.provisioning: token for "Wandpanel Küche" still pending after 30 s, retrying in 60 s – the integration did not answer the provisioning request in time',
	'2026-11-18 10:05:12 INFO hub.areas: area "Wohnzimmer" updated',
].join('\n');

export const demoPairingStatus = {
	pairing_active: true,
	pin: '4821-7730',
	remaining_seconds: 272,
	url: 'http://198.51.100.178:8099',
	deep_link: 'halumar://pair?url=http%3A%2F%2F198.51.100.178%3A8099&pin=4821-7730&tls_port=8100&fp=5a8e8ee2aaaabbbbccccddddeeeeffff00001111222233334444555660d073',
};

/** Extra endpoints the design tour needs on top of setupApiMocks' defaults. */
export function demoHandler({ path, method, json }) {
	if (method !== 'GET') return undefined;
	const area = path.match(/^\/areas\/([^/]+)$/);
	if (area) return json(demoAreaDetail(area[1]));
	const c = path.match(/^\/pairing\/clients\/([^/]+)$/);
	if (c) return json(demoClients.find((x) => x.id === c[1]) || demoClients[0]);
	const act = path.match(/^\/pairing\/clients\/([^/]+)\/activity$/);
	if (act) return json(demoActivity(act[1]));
	switch (path) {
		case '/config-templates': return json(demoTemplates);
		case '/admin/logs': return json({ logs: demoLogs });
		case '/admin/integration-status':
			return json({ connected: true, push_connected: true, integration_version: '2026.12.0-dev', addon_version: '2026.12.0-dev', ha_version: '2026.11.2', request_count: 1520, auth_failure_count: 0, last_request_ts: Date.now() / 1000 });
		case '/settings/guest-defaults': return json({ all_areas: false, tags: ['gast'], area_ids: [], template_id: null, expires_hours: 24 });
		case '/tags': return json({ tags: ['eg', 'og', 'gast', 'aussen'] });
		case '/settings/ha-urls': return json({ ha_url: 'https://home.example.ch', ha_internal_url: 'http://198.51.100.178' });
		case '/settings/app-url': return json({ app_url: '' });
		case '/settings/control-mode': return json({ control_mode: 'advanced' });
		case '/settings/notification-stacking': return json({ enabled: true });
		case '/settings/pairing-via-integration': return json({ enabled: true });
		case '/settings/compliance-endpoint': return json({ enabled: false });
		case '/settings/home': return json({ title: 'Zuhause Keller' });
		case '/settings/tls': return json({ tls_enabled: true, tls_port: 8100, fingerprint: '5a8e8ee2aaaabbbbccccddddeeeeffff00001111222233334444555660d073' });
		case '/auth/token': return json({ configured: true });
		case '/auth/integration-token': return json({ configured: true });
		case '/ha/entities': return json([]);
		default: return undefined;
	}
}

export const demoData = {
	areas: demoAreas,
	clients: demoClients,
	notifications: demoNotifications,
	health: { status: 'ok', version: '2026.12.0-dev' },
};
