import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * Family manager in the admin UI: page "Family" (open requests with
 * Yes/No, the last 24 hours, family managers, children, settings), the card
 * "Family manager" on the device page (children all/selection, HA notify
 * targets, Pro hint, "Fest" warning), the gallery suggestion "Parents" and
 * the kids rule "Darf ich noch?" (kids_requests) in the rule editor.
 * Built against the contract (openapi/hub.openapi.json tag family).
 */

const iso = (ms) => new Date(Date.now() + ms).toISOString();
const ALEX = { client_id: 'c-owner-b', name: 'iPhone Owner B', owner_label: 'Owner B' };

function request(id, category, extra = {}) {
	return {
		request_id: id,
		client_id: 'c-kid',
		child_name: 'Kid',
		owner_label: 'Kid',
		category,
		note: null,
		status: 'open',
		created_at: iso(-60_000),
		expires_at: iso(9 * 60_000),
		can_approve: true,
		answer: null,
		...extra,
	};
}

function familyState() {
	return {
		settings: { requests_enabled: true, categories: ['stay_up', 'unlock_device', 'end_pause'], request_timeout_minutes: 10, child_requests_per_hour: 6, decline_cooldown_minutes: 15, activity_days: 7 },
		open: [
			request('11111111-1111-4111-8111-111111111111', 'stay_up', { note: 'Film fertig schauen, nur noch die letzte Folge, bitte bitte' }),
			request('22222222-2222-4222-8222-222222222222', 'unlock_device', { entity_id: 'media_player.tv', entity_name: 'Fernseher Wohnzimmer mit einem sehr langen Namen' }),
			request('33333333-3333-4333-8333-333333333333', 'end_pause'),
		],
		recent: [
			{ ...request('44444444-4444-4444-8444-444444444444', 'stay_up'), status: 'approved', answer: { answer: 'yes', preset: '30m', until: iso(20 * 60_000), message: null, by: { kind: 'family_manager', client_id: 'c-owner-b', name: 'Owner B', via: 'ha_notification' }, at: iso(-30 * 60_000) } },
			{ ...request('55555555-5555-4555-8555-555555555555', 'end_pause'), status: 'expired' },
		],
		scope: { all: true, client_ids: [] },
		targets: ['notify.mobile_app_iphone_alex'],
		answers: [],
		puts: [],
	};
}

function familyHandler(fam) {
	return ({ path, method, request: req, json }) => {
		if (path === '/family' && method === 'GET') {
			return json({
				managers: [{ ...ALEX, scope: fam.scope, child_ids: ['c-kid'], notify_targets: fam.targets, remote: 'away' }],
				children: [{ client_id: 'c-kid', name: 'Kids iPad', owner_label: 'Kid', profile_name: 'Kind', fixed_manageable: ['kids_color_lock', 'kids_blocked_types'], manager_ids: ['c-owner-b'] }],
				open_requests: fam.open,
				recent_requests: fam.recent,
				settings: fam.settings,
			});
		}
		if (path === '/settings/family' && method === 'PUT') {
			fam.settings = req.postDataJSON();
			fam.puts.push(['settings', fam.settings]);
			return json(fam.settings);
		}
		const m = path.match(/^\/family\/requests\/([^/]+)\/answer$/);
		if (m && method === 'POST') {
			const body = req.postDataJSON();
			fam.answers.push([m[1], body]);
			const r = fam.open.find((x) => x.request_id === m[1]);
			if (!r) return json({ detail: { code: 'already_answered', message: 'x', request: { ...fam.recent[0] } } }, 409);
			fam.open = fam.open.filter((x) => x !== r);
			const closed = { ...r, status: body.answer === 'yes' ? 'approved' : 'declined', answer: { answer: body.answer, preset: body.preset, until: iso(30 * 60_000), message: body.message || null, by: { kind: 'admin', name: 'Owner A' }, at: iso(0) } };
			fam.recent = [closed, ...fam.recent];
			return json(closed);
		}
		if (path === '/pairing/clients/c-owner-b/family') {
			if (method === 'PUT') {
				const body = req.postDataJSON();
				fam.puts.push(['client', body]);
				fam.scope = body.scope;
				fam.targets = body.notify_targets;
			}
			return json({ client_id: 'c-owner-b', family_manager: true, scope: fam.scope, notify_targets: fam.targets, children: [] });
		}
		return undefined;
	};
}

async function boot(page, { lang = 'de', fam = familyState(), hubOpts = {} } = {}) {
	const hub = createProfilesHub(hubOpts);
	const family = familyHandler(fam);
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	await setupApiMocks(page, {
		areas: AREAS,
		handler: async (ctx) => {
			const handled = await family(ctx);
			if (handled !== undefined) return handled;
			return hub.handler(ctx);
		},
	});
	return { hub, fam, errors };
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

test.describe('Family page', () => {
	test('open requests as equal cards, answer yes with a duration and no with a text', async ({ page }) => {
		const { fam, errors } = await boot(page);
		await page.goto('/family');
		await expect(page.getByTestId('nav-family')).toBeVisible();
		const cards = page.getByTestId('family-request');
		await expect(cards).toHaveCount(3);
		await expectEqualSizes(cards);
		await expectEqualSizes(page.getByTestId('family-request').first().locator('.ractions .btn'));
		await expect(cards.nth(0)).toContainText('möchte länger wach bleiben');
		await expect(cards.nth(1)).toContainText('Fernseher Wohnzimmer');
		await expect(page.getByTestId('family-open-count')).toHaveText('3');

		await cards.nth(0).getByTestId('family-request-preset').selectOption('1h');
		await cards.nth(0).getByTestId('family-request-yes').click();
		await expect.poll(() => fam.answers.length).toBe(1);
		expect(fam.answers[0]).toEqual(['11111111-1111-4111-8111-111111111111', { answer: 'yes', preset: '1h' }]);
		await expect(page.getByTestId('family-request')).toHaveCount(2);

		const pause = page.getByTestId('family-request').filter({ hasText: 'Pause beenden' });
		await pause.getByTestId('family-request-message').selectOption('come_to_me');
		await pause.getByTestId('family-request-no').click();
		await expect.poll(() => fam.answers.length).toBe(2);
		expect(fam.answers[1][1]).toEqual({ answer: 'no', message: 'come_to_me' });
		await expect(page.getByTestId('family-recent-row').first()).toContainText('Nein');
		await expect(page.getByTestId('family-recent')).toContainText('von Owner B');
		expect(errors).toEqual([]);
	});

	test('managers, children with fixed rules and the settings', async ({ page }) => {
		const { fam } = await boot(page);
		await page.goto('/family');
		await expect(page.getByTestId('family-manager')).toContainText('Owner B · iPhone Owner B');
		await expect(page.getByTestId('family-manager')).toContainText('Alle Kinder');
		await expect(page.getByTestId('family-child')).toContainText('Betreut von Owner B');
		await expect(page.getByTestId('family-child')).toContainText('Fest im Profil');
		await page.getByTestId('family-timeout').fill('5');
		await page.getByTestId('family-settings-save').click();
		await expect.poll(() => fam.puts.length).toBe(1);
		expect(fam.puts[0][1].request_timeout_minutes).toBe(5);
	});

	test('English, mobile width without horizontal scroll', async ({ page }) => {
		await page.setViewportSize({ width: 390, height: 900 });
		await boot(page, { lang: 'en' });
		await page.goto('/family');
		await expect(page.getByRole('heading', { name: 'Requests of the children' })).toBeVisible();
		await expect(page.getByTestId('family-request').first()).toContainText('would like to stay up longer');
		const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
		expect(over).toBeLessThanOrEqual(0);
	});
});

test.describe('Family manager on the device page', () => {
	test('right on: children selection, HA target, Pro hint, save', async ({ page }) => {
		const { fam } = await boot(page, { hubOpts: { overrides: { 'c-owner-b': [{ rule: 'family_manager', value: true, user_choice: false, expires_at: null, note: null, created_by: 'Owner A', created_at: iso(-3600_000) }] } } });
		await page.goto('/clients/c-owner-b');
		const card = page.getByTestId('device-family');
		await expect(card).toBeVisible();
		await expect(card.getByTestId('device-family-state')).toHaveText('An');
		await expect(card.getByTestId('device-family-pro')).toContainText('Pro');
		await expect(card.getByTestId('device-family-target')).toHaveCount(1);
		await expect(card).toContainText('Im Profil „Kind“ von Kid sind fest');
		await card.getByTestId('device-family-mode').getByRole('radio', { name: 'Auswahl' }).click();
		await card.getByTestId('device-family-child').check();
		await card.getByTestId('device-family-target-input').fill('telegram');
		await card.getByTestId('device-family-target-add').click();
		await expect(card).toContainText('notify.<dienst>');
		await card.getByTestId('device-family-target-input').fill('notify.mobile_app_ipad_alex');
		await card.getByTestId('device-family-target-add').click();
		await card.getByTestId('device-family-save').click();
		await expect.poll(() => fam.puts.length).toBe(1);
		expect(fam.puts[0][1]).toEqual({ scope: { all: false, client_ids: ['c-kid'] }, notify_targets: ['notify.mobile_app_iphone_alex', 'notify.mobile_app_ipad_alex'] });
	});

	test('right off: hint and "adjust the right"; child and guest devices have no card', async ({ page }) => {
		await boot(page);
		await page.goto('/clients/c-owner-b');
		const card = page.getByTestId('device-family');
		await expect(card.getByTestId('device-family-state')).toHaveText('Aus');
		await card.getByTestId('device-family-grant').click();
		await expect(page.getByRole('dialog')).toBeVisible();
		await page.goto('/clients/c-kid');
		await expect(page.getByTestId('device-profile')).toBeVisible();
		await expect(page.getByTestId('device-family')).toHaveCount(0);
		await page.goto('/clients/c-visitor');
		await expect(page.getByTestId('device-profile')).toBeVisible();
		await expect(page.getByTestId('device-family')).toHaveCount(0);
	});
});

test.describe('Profiles', () => {
	test('gallery offers "Eltern", the kids rule "Darf ich noch?" can be switched', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		await expect(page.getByTestId('suggestion-parent')).toContainText('Eltern');
		await page.goto('/profiles/p-child');
		const editor = page.getByTestId('rule-value-kids_requests');
		await expect(editor).toBeVisible();
		await expect(editor.getByTestId('p-kids_requests-requests-end_pause')).toBeChecked();
		await editor.getByTestId('p-kids_requests-requests-end_pause').uncheck();
		await editor.getByTestId('p-kids_requests-requests-on').click();
		await expect(editor.getByTestId('p-kids_requests-requests-stay_up')).toHaveCount(0);
		void hub;
	});

	test('kids rule "Pause für alles": on in Kind, values editable, off hides them', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-child');
		const editor = page.getByTestId('rule-value-kids_rate_limit_all');
		await expect(editor).toBeVisible();
		await expect(editor.getByTestId('p-kids_rate_limit_all-rate-all-summary')).toHaveText('mehr als 4 Geräte oder 12 Aktionen in 1 Min.: 5 Min. Pause für alles');
		await editor.getByTestId('p-kids_rate_limit_all-rate-all-entities').fill('3');
		await expect(editor.getByTestId('p-kids_rate_limit_all-rate-all-summary')).toContainText('mehr als 3 Geräte');
		await editor.getByTestId('p-kids_rate_limit_all-rate-all-entities').fill('99');
		await expect(editor).toContainText('Geräte 1–50');
		await editor.getByTestId('p-kids_rate_limit_all-rate-all-on').click();
		await expect(editor.getByTestId('p-kids_rate_limit_all-rate-all-entities')).toHaveCount(0);
		await expect(editor.getByTestId('p-kids_rate_limit_all-rate-all-summary')).toHaveText('Aus');
	});
});
