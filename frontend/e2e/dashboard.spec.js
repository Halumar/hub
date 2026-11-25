import { test, expect } from '@playwright/test';
import { dashHub, logEntry, DEVICES, at } from './dash-hub.js';

/**
 * Overview: the setup card shrinks to one line once everything is
 * done (and opens/closes by hand, remembered), the card "Recent important"
 * shows the last important log entries with unread dots, opens the log with
 * the matching filter and follows the admin broadcasts; equal card
 * heights in a row and keyboard access.
 */
test.use({ timezoneId: 'Europe/Zurich' });

const [, KID, GUEST] = DEVICES.map((d) => d.id);

test.describe('overview: setup card', () => {
	test('complete: one line, opens and closes by hand and remembers it', async ({ page }) => {
		await dashHub(page, { lang: 'de', setupOpen: '' });
		await page.goto('/');
		const card = page.getByTestId('dash-setup');
		await expect(card).toHaveAttribute('data-state', 'collapsed');
		await expect(card).toContainText('Einrichtung abgeschlossen');
		await expect(page.getByTestId('setup-checklist')).toHaveCount(0);
		await expect(page.getByTestId('open-setup')).toHaveAttribute('href', /\/setup$/);
		await expect(page.getByTestId('open-setup')).toHaveAccessibleName('Assistent öffnen');
		const box = await card.boundingBox();
		expect(box.height).toBeLessThan(80);

		const toggle = page.getByTestId('dash-setup-toggle');
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
		await toggle.click();
		await expect(toggle).toHaveAttribute('aria-expanded', 'true');
		await expect(page.getByTestId('setup-checklist')).toBeVisible();
		await expect(card).toContainText('Einrichtung · 5 von 5 erledigt');

		await page.reload();
		await expect(page.getByTestId('dash-setup')).toHaveAttribute('data-state', 'open');
		await page.getByTestId('dash-setup-toggle').click();
		await expect(page.getByTestId('dash-setup')).toHaveAttribute('data-state', 'collapsed');
		await page.reload();
		await expect(page.getByTestId('dash-setup')).toHaveAttribute('data-state', 'collapsed');
	});

	test('keyboard: the line is a disclosure button', async ({ page }) => {
		await dashHub(page, { lang: 'en', setupOpen: '' });
		await page.goto('/');
		await expect(page.getByRole('button', { name: 'Setup complete' })).toBeVisible();
		const toggle = page.getByTestId('dash-setup-toggle');
		await toggle.focus();
		await page.keyboard.press('Enter');
		await expect(toggle).toHaveAttribute('aria-expanded', 'true');
		await expect(toggle).toHaveAttribute('aria-controls', 'dash-setup-body');
		await expect(page.locator('#dash-setup-body')).toBeVisible();
		await page.keyboard.press('Space');
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
	});

	test('incomplete: stays open without a toggle, whatever was stored', async ({ page }) => {
		await dashHub(page, { lang: 'de', complete: false, setupOpen: '' });
		await page.goto('/');
		await expect(page.getByTestId('dash-setup')).toHaveAttribute('data-state', 'open');
		await expect(page.getByTestId('setup-checklist')).toBeVisible();
		await expect(page.getByTestId('dash-setup-toggle')).toHaveCount(0);
		await expect(page.getByTestId('dash-setup')).toContainText('4 von 5 erledigt');
	});
});

test.describe('overview: recent important', () => {
	test('newest important entries of 7 days with unread dots', async ({ page }) => {
		await dashHub(page, { lang: 'de' });
		await page.goto('/');
		const items = page.getByTestId('dash-recent-item');
		await expect(items).toHaveCount(6);
		const texts = await items.allInnerTexts();
		expect(texts[0]).toContain('3 abgelehnte Versuche in 10 Min.');
		expect(texts[1]).toContain('Darf ich noch?');
		expect(texts[2]).toContain('Zugriffsgruppe wiederhergestellt');
		expect(texts[3]).toContain('Anfrage beantwortet');
		expect(texts[4]).toContain('Gast gekoppelt');
		expect(texts[5]).toContain('Profil geändert');
		// Plain activity and entries older than 7 days are not shown.
		await expect(page.getByTestId('dash-recent')).not.toContainText('Küchenlicht');
		await expect(page.getByTestId('dash-recent')).not.toContainText('Gerät gekoppelt');
		// Unread alarms (read marks of the log): dot plus "neu" for screen readers.
		await expect(page.getByTestId('dash-recent-dot')).toHaveCount(2);
		await expect(items.nth(0)).toHaveAttribute('data-unread', 'true');
		await expect(items.nth(0)).toContainText('neu');
		await expect(items.nth(1)).not.toHaveAttribute('data-unread');
		await expect(page.getByTestId('dash-recent-all')).toHaveAttribute('href', /\/log$/);
	});

	test('one request with important=true; an important change behind many others still shows', async ({ page }) => {
		const others = Array.from({ length: 250 }, (_, i) => logEntry(1000 + i, 1 + i / 100, { category: 'change', kind: 'override_set', event_kind: null, client_id: KID }));
		const paired = logEntry(900, 300, { category: 'change', kind: 'device_paired', event_kind: 'device_paired', client_id: GUEST });
		const hub = await dashHub(page, { lang: 'de', entries: [paired, ...others] });
		await page.goto('/');
		await expect(page.getByTestId('dash-recent-item')).toHaveCount(1);
		await expect(page.getByTestId('dash-recent-item').first()).toContainText('Gerät gekoppelt');
		expect(hub.logCalls).toHaveLength(1);
		expect(hub.logCalls[0]).toContain('important=true');
		expect(hub.logCalls[0]).toContain('range=7d');
	});

	test('a kids request opens the log: activity of the child, 7 days', async ({ page }) => {
		const hub = await dashHub(page, { lang: 'de' });
		await page.goto('/');
		await page.getByTestId('dash-recent-item').nth(1).click();
		await expect(page).toHaveURL(/\/log$/);
		await expect(page.getByTestId('log-kind').getByRole('button', { name: 'Aktivität' })).toHaveAttribute('aria-pressed', 'true');
		await expect(page.getByTestId('log-f-client_id')).toHaveValue(KID);
		await expect(page.getByTestId('log-range').getByRole('button', { pressed: true })).toHaveAccessibleName('7 Tage');
		await expect.poll(() => hub.logCalls.at(-1)).toContain('category=activity');
		expect(hub.logCalls.at(-1)).toContain(`client_id=${KID}`);
		expect(hub.logCalls.at(-1)).toContain('range=7d');
		await expect(page.getByTestId('log-entry').first()).toBeVisible();
	});

	test('an alarm opens the alarms of that device; a profile change the profile', async ({ page }) => {
		const hub = await dashHub(page, { lang: 'en' });
		await page.goto('/');
		await page.getByTestId('dash-recent-item').nth(2).click();
		await expect(page).toHaveURL(/\/log$/);
		await expect.poll(() => hub.logCalls.at(-1)).toContain('category=alarm');
		expect(hub.logCalls.at(-1)).toContain(`client_id=${GUEST}`);
		await page.goto('/');
		await page.getByTestId('dash-recent-item').nth(5).click();
		await expect(page).toHaveURL(/\/log$/);
		await expect.poll(() => hub.logCalls.at(-1)).toContain('category=change');
		expect(hub.logCalls.at(-1)).toContain('profile_id=p-kid');
	});

	test('live: important log entries come in, others do not reload, read marks clear the dots', async ({ page }) => {
		const hub = await dashHub(page, { lang: 'de' });
		await page.goto('/');
		await expect(page.getByTestId('dash-recent-item')).toHaveCount(6);
		await expect.poll(() => hub.socket).not.toBeNull();
		const calls = hub.logCalls.length;

		const plain = logEntry(200, 0, { client_id: KID, entity_id: 'light.kids', action: 'turn_on' });
		hub.log.state.entries.push(plain);
		hub.push({ type: 'log_entry', data: plain });
		await page.waitForTimeout(800);
		expect(hub.logCalls.length).toBe(calls);

		const soon = logEntry(201, 0, { category: 'change', kind: 'guest_expiring', event_kind: 'guest_expiring', client_id: GUEST, until: at(-60) });
		hub.log.state.entries.push(soon);
		hub.push({ type: 'log_entry', data: soon });
		await expect(page.getByTestId('dash-recent-item').first()).toContainText('Gastzugang läuft bald ab');
		await expect(page.getByTestId('dash-recent-item')).toHaveCount(6);

		hub.push({ type: 'log_read', data: { ids: null, all: true } });
		await expect(page.getByTestId('dash-recent-dot')).toHaveCount(0);
	});

	test('quiet empty state and error state', async ({ page }) => {
		await dashHub(page, { lang: 'de', entries: [] });
		await page.goto('/');
		await expect(page.getByTestId('dash-recent-empty')).toHaveText('Nichts Wichtiges in den letzten 7 Tagen');
		await expect(page.getByTestId('dash-recent-item')).toHaveCount(0);

		const page2 = await page.context().newPage();
		await dashHub(page2, { lang: 'en', failLog: true });
		await page2.goto('/');
		await expect(page2.getByTestId('dash-recent-error')).toContainText('Log not available.');
		await expect(page2.getByTestId('dash-recent-error').getByRole('button', { name: 'Reload' })).toBeVisible();
	});
});

for (const lang of ['de', 'en']) {
	for (const [vp, size] of [['desktop', { width: 1280, height: 900 }], ['wide', { width: 1600, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
		test(`equal sizes: equal cards and rows (${lang}, ${vp})`, async ({ page }) => {
			await page.setViewportSize(size);
			await dashHub(page, { lang });
			await page.goto('/');
			await expect(page.getByTestId('dash-recent-item')).toHaveCount(6);
			const recent = await page.getByTestId('dash-recent').boundingBox();
			const devices = await page.locator('section[aria-labelledby="dash-devices"]').boundingBox();
			if (vp === 'mobile') {
				expect(Math.round(recent.width)).toBe(Math.round(devices.width));
			} else {
				expect(Math.round(recent.height)).toBe(Math.round(devices.height));
				expect(Math.round(recent.y)).toBe(Math.round(devices.y));
			}
			const rows = await page.getByTestId('dash-recent-item').evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
			expect(new Set(rows).size).toBe(1);
			// Long texts are cut on one line, never wrap.
			const wrapped = await page.getByTestId('dash-recent-item').evaluateAll((els) =>
				els.flatMap((e) => [...e.querySelectorAll('b, small')]).filter((x) => x.getBoundingClientRect().height > 26).length,
			);
			expect(wrapped).toBe(0);
		});
	}
}

test('no stripes at the left edge', async ({ page }) => {
	await dashHub(page, { lang: 'de' });
	await page.goto('/');
	await expect(page.getByTestId('dash-recent-item')).toHaveCount(6);
	const stripes = await page.evaluate(() =>
		[...document.querySelectorAll('[data-testid="dash-recent"] *, [data-testid="dash-setup"] *, [data-testid="dash-recent"], [data-testid="dash-setup"]')]
			.map((el) => getComputedStyle(el))
			.filter((s) => parseFloat(s.borderLeftWidth) > 1 && s.borderLeftWidth !== s.borderRightWidth)
			.length,
	);
	expect(stripes).toBe(0);
});
