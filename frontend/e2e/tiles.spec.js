import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { demoData, demoHandler } from './design-fixtures.js';
import { TILE_AREA_ID, tileHandler } from './tile-fixtures.js';

/**
 * Tile settings panel: open by click, Enter and menu; focus handling;
 * live preview iPhone/TV × style A/B × on/off; only allowed options per
 * domain; exact PUT payloads; reset; discard; 422 at the field; run target
 * hint; "show on"; phone sheet. API mocked (tile-fixtures.js uses the real
 * contract data the hub serves).
 */
test.use({ locale: 'en-US', timezoneId: 'Europe/Zurich' });

async function open(page, state = {}) {
	const handler = tileHandler(state);
	await setupApiMocks(page, {
		...demoData,
		handler: async (ctx) => (await handler(ctx)) ?? demoHandler(ctx),
	});
	await page.goto(`/areas/${TILE_AREA_ID}`);
	await expect(page.getByRole('heading', { name: 'Wohnzimmer', level: 1 })).toBeVisible();
	return state;
}

const panel = (page) => page.getByTestId('tile-panel');
const openButton = (page, entityId) => page.locator(`[data-tile-open="${entityId}"]`);

test.describe('Tile panel', () => {
	test('click opens the panel, focus on the heading, Esc closes and returns focus', async ({ page }) => {
		await open(page);
		const tile = page.getByTestId('entity-tile').filter({ hasText: 'light.living_room_rgbww' });
		// Click on the card outside the controls (the name overlay covers the card).
		const box = await tile.locator('.tile-text small').boundingBox();
		await page.mouse.click(box.x + 10, box.y + box.height / 2);
		await expect(panel(page)).toBeVisible();
		await expect(panel(page)).toHaveAttribute('role', 'dialog');
		await expect(panel(page)).toHaveAttribute('aria-modal', 'false');
		await expect(page.getByRole('heading', { name: 'Tile “Stehlampe”' })).toBeFocused();
		await expect(openButton(page, 'light.living_room_rgbww')).toHaveAttribute('aria-current', 'true');
		await expect(page.getByTestId('tile-pv-summary')).toContainText('Stehlampe');
		await page.keyboard.press('Escape');
		await expect(panel(page)).toHaveCount(0);
		await expect(openButton(page, 'light.living_room_rgbww')).toBeFocused();
	});

	test('Enter and Space on the focused tile and the menu item open it', async ({ page }) => {
		await open(page);
		await openButton(page, 'cover.living_room_window').focus();
		await page.keyboard.press('Enter');
		await expect(page.getByRole('heading', { name: 'Tile “Store Süd”' })).toBeFocused();
		await page.getByTestId('tile-close').click();
		await openButton(page, 'light.ceiling_lights').focus();
		await page.keyboard.press(' ');
		await expect(page.getByRole('heading', { name: 'Tile “Deckenlicht”' })).toBeVisible();
		await page.getByTestId('tile-close').click();
		await page.getByRole('button', { name: 'Actions for Sonos' }).click();
		await page.getByRole('menuitem', { name: 'Customize tile …' }).click();
		await expect(page.getByRole('heading', { name: 'Tile “Sonos”' })).toBeVisible();
	});

	test('controls on the tile do not open the panel', async ({ page }) => {
		const state = await open(page);
		const tile = page.getByTestId('entity-tile').filter({ hasText: 'light.ceiling_lights' });
		const width = tile.getByRole('group', { name: 'Width of Deckenlicht' }).getByRole('button', { name: '2' });
		await expect(width).toBeEnabled();
		await width.click();
		await expect(width).toHaveAttribute('aria-pressed', 'true');
		await expect.poll(() => state.puts.length, { timeout: 15_000 }).toBe(1);
		await expect(panel(page)).toHaveCount(0);
	});

	test('live preview follows device, style and state', async ({ page }) => {
		await open(page);
		await openButton(page, 'light.living_room_rgbww').click();
		const pv = panel(page).locator('.pvroot');
		await expect(pv.locator('.app.phone .card.edit')).toBeVisible();
		await panel(page).getByTestId('tile-variant').getByRole('radio', { name: 'Slider' }).click();
		await expect(page.getByTestId('tile-size-warn')).toBeVisible();
		await page.getByRole('button', { name: 'Set size to 2×1' }).click();
		await expect(page.getByTestId('tile-size-warn')).toHaveCount(0);
		await expect(pv.locator('.card.edit .slider')).toBeVisible();
		await expect(page.getByTestId('tile-pv-summary')).toContainText('Slider');
		await panel(page).getByTestId('tile-accent').getByRole('radio', { name: 'Purple' }).click();
		await expect(pv.locator('.card.edit')).toHaveAttribute('data-t', 'media');
		await panel(page).getByTestId('tile-pv-style').getByRole('radio', { name: 'B · Color' }).click();
		await expect(pv).toHaveClass(/style-color/);
		await panel(page).getByTestId('tile-pv-state').getByRole('radio', { name: 'Off' }).click();
		await expect(pv.locator('.card.edit')).not.toHaveAttribute('data-t', /.+/);
		await panel(page).getByTestId('tile-pv-device').getByRole('radio', { name: 'Apple TV' }).click();
		await expect(pv.locator('.app.tv .card.focus')).toBeVisible();
		await expect(pv.locator('.app.tv .card.focus .level')).toBeVisible();
		await expect(page.getByTestId('tile-pv-summary')).toContainText('Apple TV');
		// Arrow keys move inside a radio group.
		await panel(page).getByTestId('tile-pv-device').getByRole('radio', { name: 'Apple TV' }).focus();
		await page.keyboard.press('ArrowLeft');
		await expect(panel(page).getByTestId('tile-pv-device').getByRole('radio', { name: 'iPhone' })).toHaveAttribute('aria-checked', 'true');
	});

	test('save sends exactly the changed fields', async ({ page }) => {
		const state = await open(page);
		await openButton(page, 'light.living_room_rgbww').click();
		await page.getByTestId('tile-icon-button').click();
		await page.getByTestId('tile-icon-search').fill('floor lamp');
		await panel(page).locator('[data-icon="floor-lamp"]').click();
		await panel(page).getByTestId('tile-variant').getByRole('radio', { name: 'Large' }).click();
		await page.getByRole('button', { name: 'Set size to 1×2' }).click();
		await panel(page).getByTestId('tile-accent').getByRole('radio', { name: 'Yellow' }).click();
		await page.getByTestId('tile-secondary').selectOption('attribute');
		await page.getByTestId('tile-tap').selectOption('toggle');
		await page.getByTestId('tile-confirm').click();
		await page.getByTestId('tile-show-on').locator('[data-ff="tv"]').uncheck();
		await page.getByTestId('tile-name').fill('  Leselampe ');
		await expect(panel(page)).toContainText('Not saved');
		await page.getByTestId('tile-save').click();
		await expect.poll(() => state.puts.length).toBe(1);
		expect(state.puts[0]).toEqual({
			entity_id: 'light.living_room_rgbww',
			body: {
				display_name: 'Leselampe',
				rows: 2,
				tile: {
					icon: 'mdi:floor-lamp',
					variant: 'large',
					secondary: { source: 'attribute', attribute: 'brightness' },
					tap_action: { action: 'toggle' },
					confirm: true,
					accent: 'light',
					hidden_on: ['tv'],
				},
			},
		});
		// Saved: badges in the editor, panel clean.
		await expect(panel(page)).not.toContainText('Not saved');
		const tile = page.getByTestId('entity-tile').filter({ hasText: 'light.living_room_rgbww' });
		await expect(tile.getByTestId('tile-badges')).toContainText('Large');
		await expect(tile.getByTestId('tile-badges')).toContainText('TV off');
	});

	test('lock: no accent, no icon, only allowed layouts and actions', async ({ page }) => {
		await open(page);
		await openButton(page, 'lock.front_door').click();
		await expect(panel(page)).toContainText('For locks the color shows the state and is fixed.');
		await expect(panel(page)).toContainText('For locks the symbol shows the state and is fixed.');
		await expect(page.getByTestId('tile-accent')).toHaveCount(0);
		await expect(page.getByTestId('tile-icon-button')).toHaveCount(0);
		await expect(panel(page).getByTestId('tile-variant').getByRole('radio')).toHaveText(['Compact', 'Large']);
		await expect(page.getByTestId('tile-tap').locator('option')).toHaveCount(3);
		await expect(page.getByTestId('tile-confirm')).toHaveCount(0);
		await expect(panel(page)).toContainText('For locks the app always asks.');
		await expect(page.getByTestId('tile-secondary').locator('option[value="entity"]')).toHaveCount(0);
	});

	test('run target: visibility hint names the devices', async ({ page }) => {
		const state = await open(page);
		await openButton(page, 'switch.tv_steckdose').click();
		await page.getByTestId('tile-tap').selectOption('run');
		await page.getByTestId('tile-target').selectOption('script.tv_abend');
		await expect(page.getByTestId('tile-scope-warn')).toContainText('“TV-Abend” is not placed in any area.');
		await expect(page.getByTestId('tile-scope-warn')).toContainText('Owner B iPhone, Gast-iPad (guest)');
		await page.getByTestId('tile-target').selectOption('scene.filmabend');
		await expect(page.getByTestId('tile-scope-warn')).toHaveCount(0);
		await expect(panel(page)).toContainText('Placed in Wohnzimmer.');
		await page.getByTestId('tile-save').click();
		await expect.poll(() => state.puts.length).toBe(1);
		expect(state.puts[0].body.tile).toEqual({
			icon: 'mdi:power-socket-ch',
			secondary: { source: 'entity', entity_id: 'sensor.tv_steckdose_power' },
			tap_action: { action: 'run', target_entity_id: 'scene.filmabend' },
		});
		expect(JSON.stringify(state.puts[0].body)).not.toContain('icon_svg');
	});

	test('reset asks and sends tile: null', async ({ page }) => {
		const state = await open(page);
		await openButton(page, 'scene.filmabend').click();
		await page.getByTestId('tile-reset').click();
		const dialog = page.getByTestId('confirm-dialog');
		await expect(dialog).toContainText('Reset all tile settings of “Filmabend” to default?');
		await dialog.getByRole('button', { name: 'Reset' }).click();
		await expect.poll(() => state.puts.length).toBe(1);
		expect(state.puts[0].body).toEqual({ tile: null });
		await expect(page.getByTestId('tile-reset')).toBeDisabled();
	});

	test('unsaved changes: Esc and switching tiles ask first', async ({ page }) => {
		await open(page);
		await openButton(page, 'light.ceiling_lights').click();
		await page.getByTestId('tile-name').fill('Decke');
		await page.keyboard.press('Escape');
		const dialog = page.getByTestId('confirm-dialog');
		await expect(dialog).toContainText('Discard the changes');
		await dialog.getByRole('button', { name: 'Cancel' }).click();
		await expect(panel(page)).toBeVisible();
		await openButton(page, 'cover.living_room_window').click();
		await dialog.getByRole('button', { name: 'Discard' }).click();
		await expect(page.getByRole('heading', { name: 'Tile “Store Süd”' })).toBeVisible();
	});

	test('422 from the hub is shown at the field', async ({ page }) => {
		await open(page, {
			fail422: [{ loc: ['body', 'tile', 'tap_action', 'target_entity_id'], msg: 'script.gute_nacht does not exist in Home Assistant', type: 'tile.target_not_found' }],
		});
		await openButton(page, 'switch.tv_steckdose').click();
		await page.getByTestId('tile-tap').selectOption('run');
		await page.getByTestId('tile-target').selectOption('script.gute_nacht');
		await page.getByTestId('tile-save').click();
		await expect(page.getByTestId('tile-target')).toHaveAttribute('aria-invalid', 'true');
		await expect(panel(page)).toContainText('does not exist in Home Assistant');
		await expect(page.getByTestId('tile-target')).toBeFocused();
	});

	test('show on: at least one form factor stays', async ({ page }) => {
		await open(page);
		await openButton(page, 'sensor.wohnzimmer_temperatur').click();
		const box = page.getByTestId('tile-show-on');
		await expect(box.locator('[data-ff="tv"]')).not.toBeChecked();
		await box.locator('[data-ff="tablet"]').uncheck();
		await expect(box.locator('[data-ff="phone"]')).toBeDisabled();
		await expect(panel(page)).toContainText('To hide the tile everywhere, use “Hide”.');
		await panel(page).getByTestId('tile-pv-device').getByRole('radio', { name: 'Apple TV' }).click();
		await expect(panel(page).locator('.app.tv .card.hiddenhere')).toContainText('Hidden on this device');
	});

	test('curated icon search and all MDI icons', async ({ page }) => {
		await open(page);
		await openButton(page, 'light.ceiling_lights').click();
		await page.getByTestId('tile-icon-button').click();
		await page.getByTestId('tile-icon-search').fill('chandelier');
		await expect(panel(page).locator('[data-icon]')).toHaveCount(1);
		await page.getByTestId('tile-icon-search').fill('ceiling fan light');
		await page.getByTestId('tile-icon-all').click();
		await expect(panel(page).getByRole('radiogroup', { name: 'MDI icons' }).getByRole('radio').first()).toBeVisible({ timeout: 20_000 });
		await panel(page).getByRole('radiogroup', { name: 'MDI icons' }).getByRole('radio').first().click();
		await expect(page.getByTestId('tile-request')).toContainText('"icon": "mdi:');
	});
});

test('every control in the panel has an accessible name', async ({ page }) => {
	await open(page);
	await openButton(page, 'switch.tv_steckdose').click();
	await page.getByTestId('tile-icon-button').click();
	await page.getByTestId('tile-tap').selectOption('run');
	await page.getByTestId('tile-secondary').selectOption('entity');
	const unnamed = await page.evaluate(() =>
		[...document.getElementById('tile-panel').querySelectorAll('button, a[href], [role="switch"], [role="radio"], input:not([type="hidden"]), select, textarea')]
			.filter((el) => el.offsetParent !== null)
			.filter((el) => {
				const label =
					el.getAttribute('aria-label') ||
					(el.getAttribute('aria-labelledby') || '')
						.split(' ')
						.map((id) => document.getElementById(id)?.textContent || '')
						.join('') ||
					(el.id && document.querySelector(`label[for="${el.id}"]`)?.textContent) ||
					el.closest('label')?.textContent ||
					el.textContent ||
					el.getAttribute('title');
				return !label || !label.trim();
			})
			.map((el) => el.outerHTML.slice(0, 120)),
	);
	expect(unnamed).toEqual([]);
	// The preview is decorative, its text summary is announced politely.
	await expect(panel(page).locator('.pvroot')).toHaveAttribute('aria-hidden', 'true');
	await expect(page.getByTestId('tile-pv-summary')).toHaveAttribute('aria-live', 'polite');
});

test.describe('Tile panel – phone', () => {
	test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

	test('full-screen sheet, no horizontal overflow, focus stays inside', async ({ page }) => {
		await open(page);
		await page.getByRole('button', { name: 'Actions for Stehlampe' }).click();
		await page.getByRole('menuitem', { name: 'Customize tile …' }).click();
		await expect(panel(page)).toHaveAttribute('aria-modal', 'true');
		const box = await panel(page).boundingBox();
		expect(box.width).toBe(390);
		const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
		expect(overflow).toBeLessThanOrEqual(0);
		for (let i = 0; i < 60; i++) await page.keyboard.press('Tab');
		const inside = await page.evaluate(() => document.getElementById('tile-panel').contains(document.activeElement));
		expect(inside).toBe(true);
	});
});

test.describe('Tile panel – German', () => {
	test.use({ locale: 'de-CH' });

	test('texts, Pro note and dative domain names', async ({ page }) => {
		await open(page);
		await openButton(page, 'lock.front_door').click();
		await expect(panel(page)).toContainText('Wird in der App mit');
		await expect(panel(page)).toContainText('Pro angezeigt');
		await expect(panel(page)).toContainText('Bei Schlössern zeigt die Farbe den Zustand und ist fest.');
		await expect(page.getByRole('heading', { name: 'Kachel „Haustür“' })).toBeVisible();
	});
});
