import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createLogHub, DEVICES, sampleEntries } from './log-hub.js';

/**
 * Log tab: timeline, filters, alarms, detail panel, settings, export,
 * notifications at events (templates, edit panel, targets, test send).
 * Mocked hub (e2e/log-hub.js). Equal sizes: rows/grids marked data-eq have equal
 * sizes in DE and EN, desktop and phone.
 */

async function open(page, path = '/log', { lang = 'de', hub = createLogHub(), viewport } = {}) {
	if (viewport) await page.setViewportSize(viewport);
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	await setupApiMocks(page, { handler: hub.handler });
	await page.goto(path);
	return hub;
}

/** Every data-eq group: children with equal width and height (±1.5 px). */
async function expectEqualGroups(page) {
	const bad = await page.evaluate(() => {
		const out = [];
		for (const group of document.querySelectorAll('[data-eq]')) {
			if (!group.offsetParent) continue;
			const kids = [...group.children].filter((el) => el.offsetParent && getComputedStyle(el).display !== 'none');
			if (kids.length < 2) continue;
			const rects = kids.map((el) => el.getBoundingClientRect());
			const w = rects.map((r) => r.width);
			const h = rects.map((r) => r.height);
			if (Math.max(...w) - Math.min(...w) > 1.5 || Math.max(...h) - Math.min(...h) > 1.5)
				out.push(`${group.getAttribute('data-eq')}: w ${w.map(Math.round)} h ${h.map(Math.round)}`);
		}
		return out;
	});
	expect(bad).toEqual([]);
}

async function noHorizontalScroll(page) {
	const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
	expect(over).toBeLessThanOrEqual(1);
}

test('navigation shows the log with the alarm badge', async ({ page }) => {
	await open(page, '/');
	const nav = page.getByTestId('nav-log');
	await expect(nav).toBeVisible();
	await expect(nav.locator('.cnt-alarm')).toHaveText('2');
	await nav.click();
	await expect(page.getByRole('heading', { level: 1, name: 'Protokoll' })).toBeVisible();
});

test('timeline grouped by day with kinds, places and results', async ({ page }) => {
	await open(page);
	await expect(page.getByTestId('log-entry')).toHaveCount(12);
	await expect(page.locator('.tl-day h2').first()).toContainText('Heute');
	const first = page.getByTestId('log-entry').first();
	await expect(first).toContainText('iPhone Air (Owner A)');
	await expect(first).toContainText('Küchenlicht turn on');
	await expect(first).toContainText('Direkt in Home Assistant');
	const denied = page.getByTestId('log-entry').filter({ hasText: 'Garagentor' });
	await expect(denied).toContainText('Nicht ausgeführt');
	await expect(denied).toContainText('Unterwegs');
	await expect(denied).toContainText('Grund: Nur anzeigen');
	const alarm = page.getByTestId('log-entry').filter({ hasText: '3 abgelehnte Versuche' });
	await expect(alarm).toHaveClass(/new/);
	await expect(alarm).toHaveAttribute('aria-label', /Alarm Abgelehnte Versuche, neu/);
	await expectEqualGroups(page);
	await noHorizontalScroll(page);
});

test('a watch removed with its phone says so', async ({ page }) => {
	const base = sampleEntries()[0];
	const removed = {
		...base,
		id: 99,
		category: 'change',
		kind: 'device_removed',
		event_kind: 'device_removed',
		source: 'hub',
		client_id: null,
		device_name: "Mum's Watch",
		owner_label: null,
		entity_id: null,
		entity_name: null,
		area_id: null,
		area_name: null,
		action: null,
		route: null,
		origin: null,
		control_path: null,
		result: null,
		reason: null,
		actor: 'Owner A',
		detail: { how: 'with_phone', with_device: { client_id: 'p1', name: "Mum's iPhone" } },
		device_exists: false,
	};
	await open(page, '/log', { lang: 'en', hub: createLogHub({ entries: [removed] }) });
	await expect(page.getByTestId('log-entry').first()).toContainText("Device removed · together with Mum's iPhone");
});

test('filters go to the hub and can be reset', async ({ page }) => {
	const hub = await open(page);
	await page.getByTestId('log-kind').getByRole('button', { name: 'Alarme' }).click();
	await expect(page.getByTestId('log-entry')).toHaveCount(3);
	expect(hub.state.requests.at(-1)).toContain('category=alarm');
	await page.getByTestId('log-f-client_id').selectOption(DEVICES[2].id);
	await expect(page.getByTestId('log-entry')).toHaveCount(1);
	await page.getByTestId('log-reset').click();
	await expect(page.getByTestId('log-entry')).toHaveCount(12);
	await page.getByTestId('log-search').fill('garagentor');
	await expect(page.getByTestId('log-entry')).toHaveCount(1);
	expect(hub.state.requests.at(-1)).toContain('q=garagentor');
	await page.getByTestId('log-reset').click();
	await page.getByTestId('log-range').getByRole('button', { name: '7 Tage' }).click();
	await expect(page.getByTestId('log-entry')).toHaveCount(10);
	await page.getByTestId('log-stat-change').click();
	expect(hub.state.requests.at(-1)).toMatch(/category=change.*range=7d|range=7d.*category=change/);
});

test('no match and empty state', async ({ page }) => {
	await open(page);
	await page.getByTestId('log-search').fill('gibtesnicht');
	await expect(page.getByTestId('log-nomatch')).toContainText('Keine Einträge für diese Filter');
	const empty = await open(page, '/log', { hub: createLogHub({ entries: [] }) });
	expect(empty.state.entries).toHaveLength(0);
	await expect(page.getByTestId('log-empty')).toContainText('Noch keine Einträge');
});

test('detail panel by keyboard, mark one alarm as read', async ({ page }) => {
	const hub = await open(page);
	const alarm = page.getByTestId('log-entry').filter({ hasText: '3 abgelehnte Versuche' });
	await alarm.focus();
	await page.keyboard.press('Enter');
	const panel = page.getByTestId('log-panel');
	await expect(panel).toBeVisible();
	await expect(panel).toContainText('Nicht in den Bereichen des Profils');
	await expect(panel).toContainText('lock.front_door');
	await panel.getByTestId('log-panel-read').click();
	expect(hub.state.requests).toContain(`POST /log/read {"ids":[2]}`);
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
	await expect(alarm).toBeFocused();
	await expect(page.getByTestId('log-unread')).toContainText('1 neuer Alarm');
	// notified targets of an activity entry
	await page.getByTestId('log-entry').filter({ hasText: 'iPad Kid · Haustür unlock' }).click();
	await expect(page.getByTestId('log-panel-sent')).toContainText('notify.mobile_app_owner_a: an Home Assistant übergeben');
	await expect(page.getByTestId('log-panel-sent')).toContainText('erscheint, sobald die App geöffnet ist');
});

test('detail panel: before → after shows only the changed values, no device row without device', async ({ page }) => {
	await open(page);
	await page.getByTestId('log-entry').filter({ hasText: 'Profil geändert' }).click();
	const panel = page.getByTestId('log-panel');
	await expect(panel.locator('.diff li')).toHaveCount(1);
	await expect(panel.locator('.diff li')).toContainText('bedtime: 20:00 → 20:30');
	await expect(panel.locator('dt', { hasText: 'Gerät' })).toHaveCount(0);
	await expect(panel.getByRole('button', { name: 'Kein Gerät' })).toBeDisabled();
});

test('alarm banner: show alarms only, mark all as read', async ({ page }) => {
	const hub = await open(page);
	const banner = page.getByTestId('log-alarm-banner');
	await expect(banner).toContainText('2 neue Alarme');
	await page.getByTestId('log-show-alarms').click();
	await expect(page.getByTestId('log-entry')).toHaveCount(3);
	await page.getByTestId('log-mark-read').click();
	expect(hub.state.requests).toContain('POST /log/read {"all":true}');
	await expect(banner).toBeHidden();
});

test('phone: last 7 days first, then load more', async ({ page }) => {
	const hub = await open(page, '/log', { viewport: { width: 390, height: 844 } });
	await expect(page.getByTestId('log-entry')).toHaveCount(10);
	expect(hub.state.requests.find((r) => r.startsWith('GET /log?'))).toContain('range=7d');
	await page.getByTestId('log-more').click();
	await expect(page.getByTestId('log-entry')).toHaveCount(12);
	await expectEqualGroups(page);
	await noHorizontalScroll(page);
});

test('settings: retention, hide people, threshold, export', async ({ page }) => {
	const hub = await open(page, '/log/settings');
	await page.getByTestId('log-retention').getByRole('button', { name: '90 Tage' }).click();
	await expect(page.getByText('Länger aufbewahren heisst auch')).toBeVisible();
	expect(hub.state.settings.retention_days).toBe(90);
	await page.getByTestId('log-hide').getByLabel('Kid').check();
	await expect.poll(() => hub.state.settings.hidden_owners).toEqual(['Kid']);
	await page.getByRole('button', { name: 'Ab 5 in 10 Min.' }).click();
	await expect.poll(() => hub.state.settings.alarm_threshold).toEqual({ count: 5, window_minutes: 10 });
	await page.getByTestId('log-export-open').click();
	const dialog = page.getByTestId('log-export');
	await expect(dialog).toContainText('Ausgeblendete Personen (Kid)');
	await dialog.getByTestId('log-export-anon').check();
	const [download] = await Promise.all([page.waitForEvent('download'), dialog.getByTestId('log-export-download').click()]);
	expect(download.suggestedFilename()).toMatch(/^protokoll-\d{4}-\d{2}-\d{2}\.csv$/);
	const text = await (await download.createReadStream()).toArray().then((c) => Buffer.concat(c).toString('utf8'));
	expect(text.charCodeAt(0)).toBe(0xfeff);
	const lines = text.slice(1).split('\r\n');
	expect(lines[0]).toBe('"Datum";"Zeit";"Art";"Alarmart";"Person";"Gerät";"Profil";"Bereich";"Ort";"Was";"Ergebnis";"Grund"');
	expect(lines.length).toBe(1 + 10); // the child's activity (2) hidden
	expect(text).not.toContain('"Owner A"');
	await expectEqualGroups(page);
});

test('notifications: toggle, edit with targets and filters, test send, save', async ({ page }) => {
	const hub = await open(page, '/log/notify');
	await expect(page.getByTestId('notify-services')).toContainText('notify.mobile_app_owner_a');
	// switching on takes the first Companion service
	await page.getByTestId('evswitch-guest_paired').click();
	await expect.poll(() => hub.state.config.rules.length).toBe(1);
	expect(hub.state.config.rules[0]).toMatchObject({ enabled: true, kinds: ['guest_paired'], targets: { notify: ['notify.mobile_app_owner_a'], devices: [] } });
	await expect(page.getByTestId('evrow-guest_paired')).toContainText('notify.mobile_app_owner_a');
	// edit denied attempts
	await page.getByTestId('evedit-alarm_denied').click();
	const panel = page.getByTestId('ev-panel');
	await expect(panel).toBeVisible();
	await panel.getByTestId('evd-on').click();
	await expect(panel.getByTestId('ev-no-target')).toBeVisible();
	await expect(panel.getByTestId('ev-save')).toBeDisabled();
	await panel.getByTestId('tgt-notify.telegram').click();
	await panel.getByTestId(`tgt-dev-${DEVICES[0].id}`).click();
	await panel.getByRole('button', { name: 'Ab 5 in 10 Min.' }).click();
	await panel.getByRole('button', { name: '10 Min. sammeln' }).click();
	await panel.getByTestId('ev-test').click();
	await expect(panel.getByTestId('ev-test-result')).toContainText('notify.telegram: an Home Assistant übergeben');
	await expect(panel.getByTestId('ev-test-result')).toContainText('iPhone Air: erscheint, sobald die App geöffnet ist');
	await expect(panel.getByTestId('ev-test-result')).toContainText('halumar_event mit test: true');
	await expectEqualGroups(page);
	await panel.getByTestId('ev-save').click();
	await expect(panel).toBeHidden();
	const rule = hub.state.config.rules.find((r) => r.kinds.includes('denied'));
	expect(rule).toMatchObject({ enabled: true, threshold: { count: 5, window_minutes: 10 }, bundle_minutes: 10, targets: { notify: ['notify.telegram'], devices: [DEVICES[0].id] } });
	expect(rule.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('notifications: child and guest devices as targets with honest hints', async ({ page }) => {
	const hub = await open(page, '/log/notify');
	await page.getByTestId('evedit-alarm_outage').click();
	const panel = page.getByTestId('ev-panel');
	await panel.getByTestId('evd-on').click();
	await expect(panel.getByTestId(`tgt-hint-${DEVICES[1].id}`)).toHaveText('Kind-Gerät · erscheint in der App, wenn das Gerät zu Hause ist');
	await expect(panel.getByTestId(`tgt-hint-${DEVICES[2].id}`)).toContainText('Gast · bis der Zugang abläuft (');
	await expect(panel.getByTestId('names-hint')).toHaveCount(0);
	await panel.getByTestId(`tgt-dev-${DEVICES[1].id}`).click();
	await panel.getByTestId(`tgt-dev-${DEVICES[2].id}`).click();
	await expect(panel.getByTestId('names-hint')).toContainText('Meldungen können Namen anderer Personen enthalten.');
	await expectEqualGroups(page);
	await panel.getByTestId('ev-save').click();
	await expect(panel).toBeHidden();
	const rule = hub.state.config.rules.find((r) => r.kinds[0] === 'integration_outage');
	expect(rule.targets.devices).toEqual([DEVICES[1].id, DEVICES[2].id]);
});

test('notifications: "everything paused" of children is off until the admin switches it on', async ({ page }) => {
	const hub = await open(page, '/log/notify');
	const row = page.getByTestId('evrow-kids_paused_all');
	await expect(row).toContainText('Alles pausiert (zu viele Geräte)');
	await expect(page.getByTestId('evswitch-kids_paused_all')).toHaveAttribute('aria-checked', 'false');
	await page.getByTestId('evedit-kids_paused_all').click();
	const panel = page.getByTestId('ev-panel');
	await expect(panel).toContainText('Höchstens eine Mitteilung pro Kind alle 15 Min.');
	await panel.getByTestId('evd-on').click();
	await panel.getByTestId('tgt-notify.telegram').click();
	await panel.getByTestId('ev-save').click();
	await expect(panel).toBeHidden();
	const rule = hub.state.config.rules.find((r) => r.kinds[0] === 'kids_paused_all');
	expect(rule).toMatchObject({ enabled: true, kinds: ['kids_paused_all'], targets: { notify: ['notify.telegram'], devices: [] } });
	// The ids of the rows before it did not move (stored rules keep their row).
	expect(rule.id).toBe('10a0e0e0-0000-4000-8000-000000000101');
});

test('notifications: template switches on, never off', async ({ page }) => {
	const hub = await open(page, '/log/notify');
	await page.getByTestId('tpl-security').click();
	const panel = page.getByTestId('tpl-panel');
	await expect(panel).toContainText('Abgelehnte Versuche');
	await panel.getByTestId('tpl-apply').click();
	await expect.poll(() => hub.state.config.rules.filter((r) => r.enabled).length).toBe(3);
	await expect(page.getByTestId('tpl-security')).toContainText('Aktiv');
	const outage = hub.state.config.rules.find((r) => r.kinds[0] === 'integration_outage');
	expect(outage.delay_minutes).toBe(5);
});

test('notifications without HA services: honest empty state', async ({ page }) => {
	await open(page, '/log/notify', { hub: createLogHub({ notify: [] }) });
	await expect(page.getByTestId('notify-services')).toContainText('Keine gefunden.');
	await page.getByTestId('evedit-dev_paired').click();
	await page.getByTestId('evd-on').click();
	await expect(page.getByTestId('ev-panel')).toContainText('Keine Benachrichtigungsdienste in Home Assistant gefunden');
});

for (const lang of ['de', 'en']) {
	for (const vp of [
		{ name: 'desktop', size: { width: 1440, height: 900 } },
		{ name: 'phone', size: { width: 390, height: 844 } },
	]) {
		test(`equal sizes and layout ${lang} ${vp.name}`, async ({ page }) => {
			for (const path of ['/log', '/log/notify', '/log/settings']) {
				await open(page, path, { lang, viewport: vp.size });
				await expect(page.locator('.logpage')).toBeVisible();
				await page.waitForTimeout(150);
				await expectEqualGroups(page);
				await noHorizontalScroll(page);
			}
			await open(page, '/log/notify', { lang, viewport: vp.size });
			await page.getByTestId('evedit-alarm_access').click();
			await page.getByTestId('evd-on').click();
			await expectEqualGroups(page);
		});
	}
}
