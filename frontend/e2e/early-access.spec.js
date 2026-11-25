import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';
import { createLogHub, sampleEntries } from './log-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * “Ask for early access” in the admin UI: the rule in the profile editor
 * (hint without access hours), the open request on the device page with the
 * admin's answer, “Access open until … (early, by Parent)” with “End early
 * access”, the log text and the notification rows. Mocked API.
 */

const inMin = (m) => new Date(Date.now() + m * 60_000).toISOString();

function request(o = {}) {
	return {
		request_id: 'b6c7d8e9-0a1b-4c2d-8e3f-4a5b6c7d8e9f',
		client_id: 'c-visitor',
		device_name: 'iPhone Visitor',
		owner_label: 'Visitor',
		profile_kind: 'guest',
		note: 'Kinder schon wach',
		status: 'open',
		created_at: inMin(-2),
		expires_at: inMin(8),
		opens_at: inMin(60),
		durations: ['until_window_start', '1h', '3h', 'today'],
		recipients: [
			{ name: 'Owner B', role: 'set_up_by' },
			{ name: 'Owner A', role: 'setup' },
		],
		answer: null,
		can_answer: true,
		...o,
	};
}

async function boot(page, { lang = 'de', open = true } = {}) {
	const hub = createProfilesHub({
		overrides: {
			'c-visitor': [{ rule: 'access_hours', value: { enabled: true, windows: [{ days: ['mon'], start: '18:00', end: '23:30' }] }, expires_at: null, user_choice: false }],
		},
		extra: (state) => {
			const visitor = state.clients.find((c) => c.id === 'c-visitor');
			visitor.access_open = false;
			visitor.access_opens_at = inMin(60);
		},
	});
	const state = { requests: open ? [request()] : [], posts: [], deletes: 0 };
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	await setupApiMocks(page, {
		areas: AREAS,
		handler: async (ctx) => {
			if (ctx.path === '/access-requests' && ctx.method === 'GET') return ctx.json({ requests: state.requests, early: [] });
			const ans = ctx.path.match(/^\/access-requests\/([^/]+)\/answer$/);
			if (ans && ctx.method === 'POST') {
				const body = ctx.request.postDataJSON();
				state.posts.push(body);
				const until = body.answer === 'yes' ? inMin(60) : null;
				const by = { kind: 'admin', client_id: null, name: 'Owner A', via: 'admin_ui' };
				state.requests = state.requests.map((r) => ({ ...r, status: body.answer === 'yes' ? 'approved' : 'declined', can_answer: false, answer: { answer: body.answer, duration: body.duration ?? null, until, message: body.message ?? null, by, at: new Date().toISOString() } }));
				if (body.answer === 'yes') hub.state.clients.find((c) => c.id === 'c-visitor').access_early = { until, by, request_id: state.requests[0].request_id, duration: body.duration, at: new Date().toISOString() };
				return ctx.json(state.requests[0]);
			}
			if (ctx.path === '/pairing/clients/c-visitor/early-access' && ctx.method === 'DELETE') {
				state.deletes += 1;
				const visitor = hub.state.clients.find((c) => c.id === 'c-visitor');
				const ended = !!visitor.access_early;
				visitor.access_early = null;
				return ctx.json({ ended });
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
	['de', { rule: 'Früher Zugang anfragen', hint: 'Wirkt nur mit Zugangszeiten.' }],
	['en', { rule: 'Ask for early access', hint: 'Only takes effect with access hours.' }],
]) {
	test(`profile editor: rule “ask for early access” with the hint without access hours (${lang})`, async ({ page }) => {
		const { errors } = await boot(page, { lang, open: false });
		await page.goto('/profiles/p-guest');
		const rule = page.getByTestId('rule-access_requests');
		await expect(rule).toContainText(txt.rule);
		await expect(rule.getByTestId('access-requests-need-hours')).toHaveText(txt.hint);
		await page.getByTestId('rule-access_hours').getByTestId('p-access_hours-access-on').click();
		await expect(rule.getByTestId('access-requests-need-hours')).toHaveCount(0);
		expect(errors).toEqual([]);
	});
}

for (const [lang, txt] of [
	['de', { title: 'Fragt nach frühem Zugang', yes1h: 'Ja, 1 Std.', no: 'Nein', to: 'Bekommen: Owner B, Owner A', answered: 'Beantwortet' }],
	['en', { title: 'Asks for early access', yes1h: 'Yes, 1 hour', no: 'No', to: 'Sent to: Owner B, Owner A', answered: 'Answered' }],
]) {
	test(`device page: open request, the admin answers Yes 1 h (${lang})`, async ({ page }) => {
		const { state, errors } = await boot(page, { lang });
		await page.goto('/clients/c-visitor');
		const box = page.getByTestId('device-access-request');
		await expect(box).toContainText(txt.title);
		await expect(box).toContainText('„Kinder schon wach“');
		await expect(box).toContainText(txt.to);
		// Equal sizes: the answer buttons are equally large.
		await expectEqualSizes(box.locator('.rel .btn'));
		await expect(page.getByTestId('device-access-yes-1h')).toHaveText(txt.yes1h);
		await page.getByTestId('device-access-yes-1h').click();
		await expect(page.getByText(txt.answered)).toBeVisible();
		expect(state.posts).toEqual([{ answer: 'yes', duration: '1h' }]);
		await expect(page.getByTestId('device-access-request')).toHaveCount(0);
		await expect(page.getByTestId('device-access-early')).toContainText('Owner A');
		expect(errors).toEqual([]);
	});
}

test('device page: early access by Parent can be ended', async ({ page }) => {
	const { hub, state, errors } = await boot(page, { open: false });
	const visitor = hub.state.clients.find((c) => c.id === 'c-visitor');
	visitor.access_early = { until: inMin(60), by: { kind: 'device', client_id: 'c-owner-b', name: 'Parent' }, request_id: null, duration: '1h', at: inMin(-1) };
	await page.goto('/clients/c-visitor');
	await expect(page.getByTestId('device-access-early')).toContainText('Vorzeitig von Parent');
	await expect(page.getByTestId('device-access-state')).toContainText('Offen bis');
	await page.getByTestId('device-access-early-end').click();
	await expect(page.getByTestId('device-access-early')).toHaveCount(0);
	expect(state.deletes).toBe(1);
	await expect(page.getByTestId('device-access-state')).toContainText('Zu bis');
	expect(errors).toEqual([]);
});

test('log: early access request and answer are readable; notification rows exist', async ({ page }) => {
	const [first] = sampleEntries();
	const base = { ...first, client_id: first.client_id, entity_id: null, entity_name: null, area_id: null, area_name: null, action: null, route: null, origin: null, control_path: null, result: null, source: 'hub' };
	const entries = [
		{ ...base, id: 101, ts: inMin(-3), category: 'activity', kind: 'access_requested', event_kind: 'access_requested', detail: { note: 'Kinder schon wach', request_id: 'r1' }, important: true },
		{ ...base, id: 102, ts: inMin(-2), category: 'change', kind: 'access_request_answered', event_kind: 'access_request_answered', action: 'yes', until: inMin(60), actor: 'Parent', detail: { duration: '1h' }, important: true },
		...sampleEntries(),
	];
	const hub = createLogHub({ entries });
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), 'de');
	await setupApiMocks(page, { handler: hub.handler });
	await page.goto('/log');
	const items = page.getByTestId('log-entry');
	await expect(items.filter({ hasText: 'Früher Zugang angefragt „Kinder schon wach“' })).toHaveCount(1);
	await expect(items.filter({ hasText: 'Anfrage nach frühem Zugang beantwortet · Ja bis' })).toHaveCount(1);
	await expect(items.filter({ hasText: 'von Parent' })).toHaveCount(1);
	await page.goto('/log/notify');
	await expect(page.getByTestId('evrow-dev_access_request')).toContainText('Früher Zugang angefragt');
	await expect(page.getByTestId('evrow-dev_access_answered')).toContainText('Anfrage beantwortet oder abgelaufen');
});
