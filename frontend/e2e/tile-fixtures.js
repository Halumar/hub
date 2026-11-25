/**
 * Fixtures for the tile panel: capabilities and curated icons are the
 * real contract data the hub serves (halumar/rootfs/opt/hub/data/*.json, kept
 * byte-identical to contracts data/), icon paths come from @mdi/js like the
 * hub's MDI table. The area mirrors the mockup (website/tile-settings-mock).
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as mdi from '@mdi/js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA = path.resolve(HERE, '../../halumar/rootfs/opt/hub/data');
const read = (f) => JSON.parse(fs.readFileSync(path.join(DATA, f), 'utf8'));
const mdiPath = (name) => mdi['mdi' + name.split('-').map((p) => p[0].toUpperCase() + p.slice(1)).join('')] || null;

export function tileCapabilities() {
	const icons = read('tile_icons.json');
	return {
		capabilities: read('tile_capabilities.json'),
		icons: { ...icons, icons: icons.icons.map((i) => ({ ...i, path: mdiPath(i.mdi) })) },
	};
}

const TS = '2026-11-18T08:15:00+00:00';
const st = (state, attributes = {}) => ({ state, attributes, last_changed: TS });
const ent = (entity_id, display_name, sort_order, state, extra = {}) => ({
	id: `ae-${entity_id}`,
	entity_id,
	display_name,
	columns: 1,
	rows: 1,
	is_visible: 1,
	control_mode: null,
	sort_order,
	tile: {},
	state,
	created_at: TS,
	...extra,
});

export const TILE_AREA_ID = 'a-wohnzimmer';

export function tileAreaDetail() {
	return {
		id: TILE_AREA_ID,
		name: 'Wohnzimmer',
		icon: 'mdi:sofa',
		description: 'Erdgeschoss',
		color: null,
		grid_cols: 2,
		is_active: true,
		sort_order: 0,
		tags: ['eg', 'gast'],
		entity_count: 9,
		control_mode: null,
		sections: [
			{
				id: 's-licht',
				title: 'Licht & Klima',
				columns: 2,
				column_span: 1,
				sort_order: 0,
				widgets: [],
				entities: [
					ent('light.ceiling_lights', 'Deckenlicht', 0, st('on', { brightness: 153, friendly_name: 'Ceiling Lights' })),
					ent('light.living_room_rgbww', 'Stehlampe', 1, st('on', { brightness: 115, friendly_name: 'Living Room RGBWW Lights' })),
					ent('climate.heatpump', 'Klima Wohnzimmer', 2, st('heat', { current_temperature: 21.5, temperature: 22, friendly_name: 'HeatPump' })),
					ent('cover.living_room_window', 'Store Süd', 3, st('open', { current_position: 70, friendly_name: 'Living Room Window' })),
				],
			},
			{
				id: 's-unterhaltung',
				title: 'Unterhaltung',
				columns: 2,
				column_span: 1,
				sort_order: 1,
				widgets: [],
				entities: [
					ent('switch.tv_steckdose', 'TV-Steckdose', 0, st('on', { friendly_name: 'TV Steckdose' }), {
						tile: {
							icon: 'mdi:power-socket-ch',
							icon_svg: mdiPath('power-socket-ch'),
							secondary: { source: 'entity', entity_id: 'sensor.tv_steckdose_power' },
							secondary_state: st('412', { unit_of_measurement: 'W' }),
						},
					}),
					ent('media_player.living_room', 'Sonos', 1, st('playing', { volume_level: 0.35, media_title: 'Epic Sax Guy', friendly_name: 'Living Room' })),
					ent('scene.filmabend', null, 2, st('2026-11-17T20:00:00+00:00', { friendly_name: 'Filmabend' }), {
						tile: { variant: 'action', accent: 'media', confirm: true },
					}),
				],
			},
			{
				id: 's-sicherheit',
				title: 'Sicherheit & Raumklima',
				columns: 2,
				column_span: 1,
				sort_order: 2,
				widgets: [],
				entities: [
					ent('lock.front_door', 'Haustür', 0, st('locked', { friendly_name: 'Front Door' })),
					ent('sensor.wohnzimmer_temperatur', 'Temperatur', 1, st('21.5', { unit_of_measurement: '°C', friendly_name: 'Wohnzimmer Temperatur' }), {
						tile: { hidden_on: ['tv'] },
					}),
				],
			},
		],
	};
}

export function tileSuggestions() {
	return {
		sensors: [
			{ entity_id: 'sensor.tv_steckdose_power', name: 'TV Steckdose Leistung', state: '412', unit: 'W', device_class: 'power', same_device: true },
			{ entity_id: 'sensor.tv_steckdose_energy', name: 'TV Steckdose Energie', state: '1.8', unit: 'kWh', device_class: 'energy', same_device: true },
			{ entity_id: 'sensor.wohnzimmer_luftfeuchte', name: 'Wohnzimmer Luftfeuchte', state: '46', unit: '%', device_class: 'humidity', same_device: false },
		],
		run_targets: [
			{ entity_id: 'scene.filmabend', name: 'Filmabend', domain: 'scene', areas: [{ area_id: TILE_AREA_ID, name: 'Wohnzimmer' }], not_visible_for: [] },
			{
				entity_id: 'script.gute_nacht',
				name: 'Gute Nacht',
				domain: 'script',
				areas: [{ area_id: 'a-schlafzimmer', name: 'Schlafzimmer' }],
				not_visible_for: [{ client_id: 'c-gast', name: 'Gast-iPad', is_guest: true, owner_label: null }],
			},
			{
				entity_id: 'script.tv_abend',
				name: 'TV-Abend',
				domain: 'script',
				areas: [],
				not_visible_for: [
					{ client_id: 'c-owner-b', name: 'Owner B iPhone', is_guest: false, owner_label: 'Owner B' },
					{ client_id: 'c-gast', name: 'Gast-iPad', is_guest: true, owner_label: null },
				],
			},
		],
		tile_viewers: [
			{ client_id: 'c-owner-b', name: 'Owner B iPhone', is_guest: false },
			{ client_id: 'c-gast', name: 'Gast-iPad', is_guest: true },
		],
	};
}

/**
 * Handler for setupApiMocks: area detail (mutated by PUTs, like the hub),
 * capabilities, suggestions. `puts` collects PUT bodies; `fail422` makes the
 * next PUT answer 422 with the given detail.
 */
export function tileHandler(state = {}) {
	state.area = state.area || tileAreaDetail();
	state.puts = state.puts || [];
	return async ({ path, method, json, request }) => {
		if (path === '/tiles/capabilities' && method === 'GET') return json(tileCapabilities());
		if (path.endsWith('/tile/suggestions') && method === 'GET') return json(tileSuggestions());
		if (path === `/areas/${TILE_AREA_ID}` && method === 'GET') return json(state.area);
		const m = path.match(/^\/areas\/[^/]+\/entities\/([^/]+)$/);
		if (m && method === 'PUT') {
			const body = request.postDataJSON();
			state.puts.push({ entity_id: decodeURIComponent(m[1]), body });
			if (state.fail422) {
				const detail = state.fail422;
				state.fail422 = null;
				return json({ detail }, 422);
			}
			for (const s of state.area.sections) {
				const e = s.entities.find((x) => x.entity_id === decodeURIComponent(m[1]));
				if (!e) continue;
				if ('tile' in body) {
					e.tile = body.tile ? { ...body.tile } : {};
					if (e.tile.icon?.startsWith('mdi:')) e.tile.icon_svg = mdiPath(e.tile.icon.slice(4));
				}
				for (const k of ['display_name', 'columns', 'rows']) if (k in body) e[k] = body[k];
				return json({ ...e });
			}
			return json({ detail: 'Entity not found in area' }, 404);
		}
		return undefined;
	};
}
