import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';

/**
 * Whole list rows/cards open their detail (stretched link), row
 * controls never navigate; "Properties" moved into the "…" menu; guests page
 * layout and guest area scope (guests fail closed, "All areas" is explicit).
 */

const H = 3600_000;
const iso = (ms) => new Date(Date.now() + ms).toISOString();

const guest = (o) => ({
	is_active: true, is_connected: true, is_guest: true, tags: ['gast'], created_at: iso(-24 * H), last_seen: iso(-60_000),
	has_ha_token: true, ha_user_id: 'u', ha_token_status: 'active', ha_token_pending_since: null, owner_label: null,
	remote_allowed: false, expires_at: iso(48 * H), pending_deletion: false, ...o,
});

const GUESTS = [
	guest({ id: 'g-active', name: 'Owner B iPhone', owner_label: 'Owner B' }),
	guest({ id: 'g-soon', name: 'Sitters iPhone', owner_label: 'Sitter', expires_at: iso(H), tags: [] }),
	guest({ id: 'g-expired', name: 'Guest iPad', owner_label: 'Guest', expires_at: iso(-H), created_at: iso(-49 * H), tags: ['*'] }),
	guest({ id: 'g-setup', name: 'Gast mit einem sehr langen Gerätenamen, der nicht in die Karte passt – iPhone 17 Pro Max', owner_label: 'Gast mit einem sehr langen Namen für die Kartenbreite', ha_token_status: 'pending', has_ha_token: false, ha_user_id: null }),
];

function tagsHandler({ path, json }) {
	if (path === '/tags') return json({ tags: ['gast', 'og', 'eg'] });
	if (path === '/settings/guest-defaults') return json({ all_areas: false, tags: ['gast'], area_ids: [], template_id: null, expires_hours: 24 });
	return undefined;
}

test.describe('Area list: whole row opens the editor', () => {
	test('clicking the row (not only the name) opens the editor', async ({ page }) => {
		await setupApiMocks(page);
		await page.goto('/areas');
		const row = page.getByTestId('area-row').first();
		await row.getByText('Main lounge').click({ force: true }); // lands on the row overlay
		await expect(page).toHaveURL(/\/areas\/area-living-room$/);
		await page.goBack();
		await expect(page.getByRole('link', { name: 'Kitchen' })).toBeVisible();
		await page.getByTestId('area-row').nth(1).locator('.rowlink-chev').click({ force: true });
		await expect(page).toHaveURL(/\/areas\/area-kitchen$/);
	});

	test('the row is one real link; no separate "Edit" button', async ({ page }) => {
		await setupApiMocks(page);
		await page.goto('/areas');
		const row = page.getByTestId('area-row').first();
		await expect(row.getByRole('link')).toHaveCount(1);
		await expect(row.getByRole('link', { name: 'Living Room' })).toHaveAttribute('href', /\/areas\/area-living-room$/);
		await expect(row.getByRole('button', { name: /Edit/ })).toHaveCount(0);
	});

	test('switch, reorder buttons, menu and the control strip do not navigate', async ({ page }) => {
		const requests = [];
		await setupApiMocks(page, { requests });
		await page.goto('/areas');
		const row = page.getByTestId('area-row').first();

		await row.getByRole('switch').click();
		await expect.poll(() => requests.filter((r) => r.startsWith('PATCH /areas/area-living-room')).length).toBe(1);
		await row.getByRole('button', { name: /Move Living Room down/ }).click();
		await row.getByTestId('area-controls').click({ position: { x: 2, y: 2 } });
		await page.getByTestId('area-row').nth(1).getByRole('button', { name: 'Actions for Kitchen' }).click();
		await expect(page.getByRole('menuitem', { name: 'Properties' })).toBeVisible();
		await page.keyboard.press('Escape');
		await page.waitForTimeout(200);
		await expect(page).toHaveURL(/\/areas$/);
	});

	test('"Properties" in the menu opens the properties dialog without navigating', async ({ page }) => {
		await setupApiMocks(page);
		await page.goto('/areas');
		await page.getByRole('button', { name: 'Actions for Kitchen' }).click();
		await page.getByRole('menuitem', { name: 'Properties' }).click();
		const dialog = page.getByRole('dialog', { name: 'Area properties' });
		await expect(dialog).toBeVisible();
		await expect(dialog.getByRole('textbox', { name: 'Name', exact: true })).toHaveValue('Kitchen');
		await expect(page).toHaveURL(/\/areas$/);
	});

	test('keyboard: focus ring on the row, Enter and Space open the editor', async ({ page }) => {
		await setupApiMocks(page);
		await page.goto('/areas');
		const link = page.getByRole('link', { name: 'Living Room' });
		await link.focus();
		await page.keyboard.press('Shift+Tab');
		await page.keyboard.press('Tab');
		const row = page.getByTestId('area-row').first();
		await expect.poll(() => row.evaluate((el) => getComputedStyle(el).outlineStyle)).toBe('solid');
		await page.keyboard.press('Enter');
		await expect(page).toHaveURL(/\/areas\/area-living-room$/);

		await page.goto('/areas');
		await page.getByRole('link', { name: 'Kitchen' }).focus();
		await page.keyboard.press('Space');
		await expect(page).toHaveURL(/\/areas\/area-kitchen$/);
	});

	test('hover highlights only over the navigable part', async ({ page }) => {
		await setupApiMocks(page);
		await page.goto('/areas');
		const row = page.getByTestId('area-row').first();
		const bg = () => row.evaluate((el) => getComputedStyle(el).backgroundColor);
		await row.getByText('Main lounge').hover({ force: true });
		const hovered = await bg();
		await row.getByRole('switch').hover();
		expect(await bg()).not.toBe(hovered);
	});

	test('editor header: "Properties" opens the same dialog', async ({ page }) => {
		await setupApiMocks(page);
		await page.goto('/areas/area-kitchen');
		await page.getByRole('button', { name: 'Properties', exact: true }).click();
		await expect(page.getByRole('dialog', { name: 'Area properties' })).toBeVisible();
	});
});

test.describe('Area list on a phone (touch)', () => {
	test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

	test('tapping the card opens the editor, tapping controls does not', async ({ page }) => {
		const requests = [];
		await setupApiMocks(page, { requests });
		await page.goto('/areas');
		const row = page.getByTestId('area-row').first();
		await row.getByRole('switch').tap();
		await expect.poll(() => requests.filter((r) => r.startsWith('PATCH')).length).toBe(1);
		await row.getByRole('button', { name: 'Actions for Living Room' }).tap();
		await expect(page.getByRole('menuitem', { name: 'Properties' })).toBeVisible();
		await page.getByRole('menuitem', { name: 'Copy ID' }).tap();
		await expect(page).toHaveURL(/\/areas$/);
		const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
		expect(overflow).toBeLessThanOrEqual(0);
		await row.getByText('Main lounge').tap({ force: true });
		await expect(page).toHaveURL(/\/areas\/area-living-room$/);
	});
});

