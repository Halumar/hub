import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * Wall panel as a Home Assistant device in the admin UI: the panel
 * card on the device page (status, battery, entities in HA, test buttons of
 * equal size, contract error codes, blueprint hint) and the panel rules in
 * the profile editor (values in HA, wake from sensors, brightness by light).
 * Built against the contract (rest/client-panel.response.json,
 * integration/panel-command.*).
 */

const PANEL = {
	screen: 'dimmed',
	brightness: 0.35,
	brightness_mode: 'auto',
	actions: ['wake', 'screensaver', 'home', 'reload', 'set_brightness', 'set_brightness_mode'],
};

function cardOf(id, extra = {}) {
	const tv = id === 'c-tv';
	return {
		client_id: id,
		platform: tv ? 'tvos' : 'ipados',
		is_connected: true,
		is_panel: true,
		panel: tv ? { screen: 'active', brightness: null, actions: ['wake', 'screensaver', 'home', 'reload'] } : PANEL,
		battery_level: tv ? null : 64,
		battery_state: tv ? 'unknown' : 'charging',
		low_power: false,
		panel_ha: tv
			? { sensors: ['screen'], controls: ['wake', 'screensaver', 'home', 'reload'] }
			: { sensors: ['battery', 'charging', 'screen', 'brightness'], controls: ['brightness', 'brightness_mode', 'wake', 'screensaver', 'home', 'reload'] },
		remote: true,
		supported_actions: tv ? ['wake', 'screensaver', 'home', 'reload'] : PANEL.actions,
		updated_at: new Date(Date.now() - 60_000).toISOString(),
		...extra,
	};
}

async function boot(page, { lang = 'de', card = {}, fail = null } = {}) {
	const hub = createProfilesHub();
	const commands = [];
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	await setupApiMocks(page, {
		areas: AREAS,
		handler: async (ctx) => {
			const m = ctx.path.match(/^\/pairing\/clients\/([^/]+)\/panel$/);
			if (m && ctx.method === 'GET') {
				// The iPhone reports its status too – sensors, no control.
				if (m[1] === 'c-owner-a')
					return ctx.json({
						...cardOf(m[1]),
						platform: 'ios',
						is_panel: false,
						panel: { screen: 'active', panel_mode: false, brightness: 0.62, actions: [] },
						battery_level: 52,
						battery_state: 'unplugged',
						panel_ha: { sensors: ['battery', 'charging', 'app', 'brightness'], controls: [] },
						remote: false,
						supported_actions: [],
						...(card.phone || {}),
					});
				return ctx.json(cardOf(m[1], card));
			}
			if (m && ctx.method === 'POST') {
				const body = ctx.request.postDataJSON();
				commands.push([m[1], body]);
				if (fail) return ctx.json({ detail: { code: fail, message: fail } }, fail === 'timeout' ? 504 : 422);
				return ctx.json({ success: true, client_id: m[1], action: body.action, panel: { ...PANEL, screen: body.action === 'screensaver' ? 'screensaver' : 'active' } });
			}
			return hub.handler(ctx);
		},
	});
	return { hub, commands, errors };
}

/** Equal sizes: every element of the locator has the same width and height (±1.5 px). */
async function expectEqualSizes(locator) {
	const boxes = await locator.evaluateAll((els) => els.map((e) => e.getBoundingClientRect()).map((r) => ({ w: r.width, h: r.height })));
	expect(boxes.length).toBeGreaterThan(1);
	for (const b of boxes) {
		expect(Math.abs(b.w - boxes[0].w)).toBeLessThanOrEqual(1.5);
		expect(Math.abs(b.h - boxes[0].h)).toBeLessThanOrEqual(1.5);
	}
}

test.describe('Panel card', () => {
	test('iPad: status, entities in HA, equal test buttons, wake and brightness', async ({ page }) => {
		const { commands, errors } = await boot(page);
		await page.goto('/clients/c-panel');
		const card = page.getByTestId('panel-card');
		await expect(card).toBeVisible();
		await expect(card.getByTestId('panel-screen')).toHaveText('Gedimmt');
		await expect(card.getByTestId('panel-battery')).toContainText('64 %');
		await expect(card.getByTestId('panel-battery')).toContainText('lädt');
		await expect(card.getByTestId('panel-in-ha')).toContainText('Akku, Laden, Bildschirm, Helligkeit');
		await expect(card.getByTestId('panel-blueprint')).toContainText('Akkuschutz 20–80 %');
		await expectEqualSizes(card.locator('.btn-grid .btn'));

		await card.getByTestId('panel-btn-wake').click();
		await expect.poll(() => commands.length).toBe(1);
		expect(commands[0]).toEqual(['c-panel', { action: 'wake' }]);
		await expect(card.getByTestId('panel-screen')).toHaveText('Aktiv');

		await card.getByTestId('panel-btn-brightness').click();
		await expect.poll(() => commands.length).toBe(2);
		expect(commands[1][1]).toEqual({ action: 'set_brightness', level: 0.35 });
		expect(errors).toEqual([]);
	});

	test('error codes of the contract become clear messages', async ({ page }) => {
		await boot(page, { fail: 'not_pro' });
		await page.goto('/clients/c-panel');
		await page.getByTestId('panel-btn-home').click();
		await expect(page.getByText('Das Wandpanel braucht Pro auf dem Gerät.')).toBeVisible();
	});

	test('Apple TV: screen and four buttons, no brightness, no blueprint (English)', async ({ page }) => {
		await boot(page, { lang: 'en' });
		await page.goto('/clients/c-tv');
		const card = page.getByTestId('panel-card');
		await expect(card).toContainText('Wall panel in Home Assistant');
		await expect(card.locator('.btn-grid .btn')).toHaveCount(4);
		await expect(card.getByTestId('panel-btn-brightness')).toHaveCount(0);
		await expect(card.getByTestId('panel-blueprint')).toHaveCount(0);
	});

	test('control from HA off: no test buttons', async ({ page }) => {
		await boot(page, { card: { remote: false, panel_ha: { sensors: ['screen'], controls: [] } } });
		await page.goto('/clients/c-panel');
		const card = page.getByTestId('panel-card');
		await expect(card).toContainText('Steuerung aus Home Assistant ist aus');
		await expect(card.locator('.btn-grid')).toHaveCount(0);
	});

	test('iPhone: app active, brightness, battery and sensors in HA, never control', async ({ page }) => {
		const { errors } = await boot(page);
		await page.goto('/clients/c-owner-a');
		await expect(page.getByTestId('ha-user-card')).toBeVisible();
		const card = page.getByTestId('panel-card');
		await expect(card).toContainText('Gerät in Home Assistant');
		await expect(card.getByTestId('panel-app')).toHaveText('Im Vordergrund');
		await expect(card.getByTestId('panel-brightness')).toContainText('62 %');
		await expect(card.getByTestId('panel-battery')).toContainText('52 %');
		await expect(card.getByTestId('panel-in-ha')).toHaveText('Akku, Laden, App aktiv, Helligkeit');
		await expect(card.getByTestId('panel-not-panel')).toContainText('steuert es aber nicht');
		await expect(card.locator('.btn-grid')).toHaveCount(0);
		await expect(card.getByTestId('panel-blueprint')).toHaveCount(0);
		expect(errors).toEqual([]);
	});

	test('iPhone in the background and offline (English)', async ({ page }) => {
		await boot(page, { lang: 'en', card: { phone: { panel: { screen: 'off', panel_mode: false, brightness: null, actions: [] } } } });
		await page.goto('/clients/c-owner-a');
		const card = page.getByTestId('panel-card');
		await expect(card).toContainText('Device in Home Assistant');
		await expect(card.getByTestId('panel-app')).toHaveText('Not in front');
		await expect(card.getByTestId('panel-brightness')).toContainText('–');
		await expect(card.getByTestId('panel-in-ha')).toHaveText('Battery, Charging, App active, Brightness');
	});
});

test.describe('Panel rules in the profile editor', () => {
	test('values in HA, wake from sensors and brightness by light', async ({ page }) => {
		const { errors } = await boot(page);
		await page.goto('/profiles/p-wall');
		const sensors = page.getByTestId('rule-value-panel_sensors');
		await expect(sensors).toBeVisible();
		await expect(sensors.getByTestId('p-panel_sensors-sensor-battery')).toHaveAttribute('aria-pressed', 'true');
		await expectEqualSizes(sensors.locator('.tchip'));
		await sensors.getByTestId('p-panel_sensors-sensor-brightness').click();
		await expect(sensors.getByTestId('p-panel_sensors-sensor-brightness')).toHaveAttribute('aria-pressed', 'false');

		const wake = page.getByTestId('rule-value-panel_wake');
		await expect(wake).toContainText('Nur Berührung');

		const bright = page.getByTestId('rule-value-panel_brightness');
		await bright.getByRole('radio', { name: 'Nach Licht' }).click();
		await expect(bright.getByTestId('p-panel_brightness-lux-entity')).toBeVisible();
		await expect(bright).toContainText('Ohne Sensor regelt das Panel nicht nach Licht.');
		expect(errors).toEqual([]);
	});
});
