import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * Area page › Room climate. Automatic shows what the apps use (the
 * fridge does not count), the admin may pick one entity per measure (PUT room-climate), narrow
 * screens show the card below the sections. Mocked API.
 */

function option(entity_id, name, value, excluded = null, tier = 'sensor') {
	return { entity_id, name, tier, in_room: true, excluded, value };
}

const FRIDGE = option('sensor.sensor_1_temperature', 'Sensor 1 Temperatur', 4.2, 'fridge');
const ROOM = option('sensor.kitchen_temperature', 'Raumtemperatur', 21.4);
const HUM = option('sensor.kitchen_humidity', 'Luftfeuchte', 55);

function setting(options, chosen, value, unit) {
	const auto = options.filter((o) => !o.excluded).map((o) => o.entity_id);
	return {
		entity_id: chosen,
		pick: chosen ? { source: 'admin', entity_ids: [chosen] } : { source: 'auto', entity_ids: auto },
		value,
		unit,
		options,
	};
}

function hub() {
	const state = { temperature: null, humidity: null, puts: [] };
	const answer = () => ({
		temperature: setting([FRIDGE, ROOM], state.temperature, state.temperature ? 4.2 : 21.4, '°C'),
		humidity: setting([HUM], state.humidity, 55, '%'),
	});
	const handler = async ({ path, method, request, json }) => {
		if (!/^\/areas\/[^/]+\/room-climate$/.test(path)) return undefined;
		if (method === 'PUT') {
			const body = JSON.parse(request.postData() || '{}');
			state.puts.push(body);
			state.temperature = body.temperature_entity_id;
			state.humidity = body.humidity_entity_id;
		}
		return json(answer());
	};
	return { state, handler };
}

async function open(page, lang, mock) {
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	await setupApiMocks(page, mock ? { handler: mock.handler } : {});
	await page.goto('/areas/area-living-room');
}

test('automatic leaves the fridge out, picking it is saved (de)', async ({ page }) => {
	const mock = hub();
	await open(page, 'de', mock);
	const card = page.getByTestId('room-climate');
	await expect(card.getByRole('heading', { name: 'Raumklima' })).toBeVisible();
	const select = page.getByTestId('room-climate-temperature');
	await expect(select).toHaveValue('');
	await expect(select.locator('option').first()).toHaveText('Automatisch (Raumtemperatur)');
	await expect(select.locator('option').nth(1)).toHaveText('Sensor 1 Temperatur – zählt nicht automatisch: Kühlschrank');
	await expect(page.getByTestId('room-climate-temperature-value')).toHaveText('Zeigt: 21,4 °C');
	await expect(page.getByTestId('room-climate-humidity-value')).toHaveText('Zeigt: 55 %');

	await select.selectOption('sensor.sensor_1_temperature');
	await expect(page.getByTestId('room-climate-temperature-value')).toHaveText('Zeigt: 4,2 °C');
	expect(mock.state.puts).toEqual([{ temperature_entity_id: 'sensor.sensor_1_temperature', humidity_entity_id: null }]);

	await select.selectOption('');
	await expect(page.getByTestId('room-climate-temperature-value')).toHaveText('Zeigt: 21,4 °C');
	expect(mock.state.puts.at(-1)).toEqual({ temperature_entity_id: null, humidity_entity_id: null });
});

test('an area without sensors says so (en)', async ({ page }) => {
	await open(page, 'en');
	await expect(page.getByTestId('room-climate').getByRole('heading', { name: 'Room climate' })).toBeVisible();
	await expect(page.getByTestId('room-climate-temperature')).toHaveCount(0);
	await expect(page.getByTestId('room-climate-temperature-value')).toHaveText('No suitable sensor');
	await expect(page.getByTestId('room-climate-humidity-value')).toHaveText('No suitable sensor');
});

test('narrow screens show the card without the schematic preview', async ({ page }) => {
	await page.setViewportSize({ width: 420, height: 900 });
	await open(page, 'de', hub());
	await expect(page.getByTestId('room-climate-temperature')).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Vorschau iPhone' })).toBeHidden();
});
