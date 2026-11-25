// Home Assistant domain metadata for the area screens. Labels are i18n keys
// (t(`areas.domains.<id>`), t(`editor.widgetTypes.<value>.label`)).

/** Domains offered as filters when importing/adding entities. */
export const availableDomains = [
	'light',
	'switch',
	'button',
	'sensor',
	'binary_sensor',
	'climate',
	'cover',
	'fan',
	'media_player',
	'lock',
	'camera',
	'vacuum',
].map((id) => ({ id }));

/** Stroke icon ($lib/ui/icons.js) per domain. */
const DOMAIN_ICON = {
	light: 'bulb',
	switch: 'power',
	button: 'power',
	input_boolean: 'power',
	sensor: 'sensor',
	binary_sensor: 'sensor',
	climate: 'climate',
	water_heater: 'thermo',
	cover: 'blinds',
	fan: 'fan',
	media_player: 'music',
	lock: 'lock',
	camera: 'camera',
	vacuum: 'home',
	scene: 'scene',
	script: 'scene',
	alarm_control_panel: 'shield',
	weather: 'sun',
};

/** Icon name for a domain id or an entity_id ("light.kitchen"). */
export function domainIcon(idOrEntity) {
	const domain = String(idOrEntity || '').split('.')[0];
	return DOMAIN_ICON[domain] || 'grid';
}

/** Non-entity widget types. */
export const WIDGET_TYPES = [{ value: 'clock' }, { value: 'text' }, { value: 'spacer' }];
