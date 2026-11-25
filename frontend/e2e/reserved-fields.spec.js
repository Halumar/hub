import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * The reserved wall
 * panel rules in the profile editor (announcements, screen off, proximity,
 * battery temperature), the panel card with capabilities and battery
 * temperature, and the NFC/QR tag card in Settings › General. Mocked API.
 */

async function boot(page, { lang = 'de', card = null, tags = [] } = {}) {
	const hub = createProfilesHub();
	const state = { tags: [...tags], posts: [] };
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	await setupApiMocks(page, {
		areas: AREAS,
		handler: async (ctx) => {
			if (card && /^\/pairing\/clients\/[^/]+\/panel$/.test(ctx.path) && ctx.method === 'GET') return ctx.json(card);
			if (ctx.path === '/scan-tags' && ctx.method === 'GET') return ctx.json({ tags: state.tags, max: 100 });
			if (ctx.path === '/scan-tags' && ctx.method === 'POST') {
				const body = ctx.request.postDataJSON();
				state.posts.push(body);
				if (body.action.entity_id === 'light.x') return ctx.json({ detail: { code: 'invalid_target', message: 'no' } }, 422);
				const tag = { tag_id: 'q8Zr3vT1yL0pXw5nB2cD4e', name: body.name, action: { entity_id: null, area_id: null, ...body.action }, url: 'https://halumar.com/t/q8Zr3vT1yL0pXw5nB2cD4e', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), last_scanned_at: null };
				state.tags = [tag, ...state.tags];
				return ctx.json(tag, 201);
			}
			const del = ctx.path.match(/^\/scan-tags\/([^/]+)$/);
			if (del && ctx.method === 'DELETE') {
				state.tags = state.tags.filter((t) => t.tag_id !== decodeURIComponent(del[1]));
				return ctx.json({ success: true });
			}
			return hub.handler(ctx);
		},
	});
	return { hub, state, errors };
}

async function expectEqualSizes(locator) {
	const boxes = await locator.evaluateAll((els) => els.map((e) => e.getBoundingClientRect()).map((r) => ({ w: r.width, h: r.height })));
	expect(boxes.length).toBeGreaterThan(1);
	for (const b of boxes) {
		expect(Math.abs(b.w - boxes[0].w)).toBeLessThanOrEqual(1.5);
		expect(Math.abs(b.h - boxes[0].h)).toBeLessThanOrEqual(1.5);
	}
}

for (const [lang, txt] of [
	['de', { night: 'Im Nachtmodus nur als Text', prox: 'Auch durch Näherung wecken (Android)', black: 'Schwarzbild', temp: 'Akkutemperatur (Android)' }],
	['en', { night: 'Text only during night mode', prox: 'Also wake by proximity (Android)', black: 'Black screen', temp: 'Battery temperature (Android)' }],
]) {
	test(`wall panel profile: announcements, screen off, proximity, battery temperature (${lang})`, async ({ page }) => {
		const { errors } = await boot(page, { lang });
		await page.goto('/profiles/p-wall');
		const announce = page.getByTestId('rule-value-panel_announce');
		await expect(announce).toBeVisible();
		await expect(announce.getByTestId('p-panel_announce-announce-volume')).toHaveValue('80');
		await expect(announce.getByText(txt.night)).toBeVisible();
		await announce.getByTestId('p-panel_announce-announce-volume').selectOption('60');
		await expect(announce.getByTestId('p-panel_announce-announce-volume')).toHaveValue('60');

		const off = page.getByTestId('rule-value-panel_off_mode');
		await expect(off.getByRole('radio', { name: txt.black })).toBeChecked();

		const wake = page.getByTestId('rule-value-panel_wake');
		await expect(wake.getByText(txt.prox)).toBeVisible();
		await wake.getByTestId('p-panel_wake-wake-proximity').locator('input').check();

		const sensors = page.getByTestId('rule-value-panel_sensors');
		await expect(sensors.getByTestId('p-panel_sensors-sensor-battery_temperature')).toHaveAttribute('aria-pressed', 'true');
		await expect(sensors.getByText(txt.temp)).toBeVisible();
		await expectEqualSizes(sensors.locator('.tchip'));
		expect(errors).toEqual([]);
	});
}

test('panel card: battery temperature and capabilities (Android tablet)', async ({ page }) => {
	const card = {
		client_id: 'c-panel',
		platform: 'android_tablet',
		is_connected: true,
		is_panel: true,
		panel: { screen: 'active', brightness: 0.5, brightness_mode: 'auto', actions: ['wake', 'announce'], pip: true, nfc: false, device_admin: true, charge_limit_detected: true, battery_temperature: 31.46, battery_health: 'good' },
		battery_level: 80,
		battery_state: 'charging',
		low_power: false,
		panel_ha: { sensors: ['battery', 'charging', 'screen', 'brightness', 'battery_temperature'], controls: ['wake'] },
		remote: true,
		supported_actions: ['wake', 'announce'],
		updated_at: new Date().toISOString(),
	};
	const { errors } = await boot(page, { card });
	await page.goto('/clients/c-panel');
	const panel = page.getByTestId('panel-card');
	await expect(panel.getByTestId('panel-battery-temperature')).toHaveText('31.5 °C · gut');
	const caps = panel.getByTestId('panel-capabilities');
	await expect(caps).toContainText('Bild-im-Bild: aktiv');
	await expect(caps).toContainText('NFC: fehlt');
	await expect(caps).toContainText('Geräteadministrator: aktiv');
	await expect(panel.getByTestId('panel-in-ha')).toContainText('Akkutemperatur');
	await expect(panel.getByTestId('panel-blueprint')).toContainText('begrenzt das Laden selbst');
	expect(errors).toEqual([]);
});

for (const [lang, txt] of [
	['de', { title: 'NFC- und QR-Tags', empty: 'Noch keine Tags.', err: 'Gerät oder Bereich passt nicht zur Aktion.' }],
	['en', { title: 'NFC and QR tags', empty: 'No tags yet.', err: 'The device or area does not fit the action.' }],
]) {
	test(`settings: add, show and remove an NFC tag (${lang})`, async ({ page }) => {
		const { state, errors } = await boot(page, { lang });
		await page.goto('/settings');
		const card = page.getByTestId('scan-tags-card');
		await expect(card.getByRole('heading', { name: txt.title })).toBeVisible();
		await expect(card.getByTestId('scan-tags-empty')).toHaveText(txt.empty);
		await card.getByTestId('scan-tag-name').fill('Flur Licht');
		await card.getByTestId('scan-tag-target').fill('light.x');
		await card.getByTestId('scan-tag-add').click();
		await expect(card.getByText(txt.err)).toBeVisible();
		await card.getByTestId('scan-tag-target').fill('light.hallway');
		await card.getByTestId('scan-tag-add').click();
		await expect(card.getByTestId('scan-tags-list').locator('li')).toHaveCount(1);
		expect(state.posts.at(-1)).toEqual({ name: 'Flur Licht', action: { kind: 'toggle', entity_id: 'light.hallway' } });
		await expect(card).toContainText('https://halumar.com/t/q8Zr3vT1yL0pXw5nB2cD4e');
		await card.getByTestId('scan-tag-remove').click();
		await expect(card.getByTestId('scan-tags-empty')).toBeVisible();
		expect(errors).toEqual([]);
	});
}
