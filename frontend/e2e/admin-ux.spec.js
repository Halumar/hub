import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * Admin UI UX: change many devices together (selection, dialog,
 * summary from the hub's dry run, apply, result), template "apply to
 * devices", batch reorder of areas and the audit view. Mocked API.
 */

const ago = (min) => new Date(Date.now() - min * 60000).toISOString();
const base = {
	is_active: true,
	has_ha_token: true,
	ha_token_status: 'active',
	ha_token_pending_since: null,
	created_at: '2026-11-01T10:00:00Z',
	last_seen: ago(95),
	protocol_version: 1,
	app_version: '2026.12.0',
	update_required: false,
	remote_allowed: false,
	can_invite_guests: false,
	tags: [],
};
const DEVICES = [
	{ ...base, id: 'c-ipad', name: 'iPad Flur', owner_label: null, platform: 'ipados', device_model: 'iPad16,3', is_connected: true, is_guest: false },
	{ ...base, id: 'c-tv', name: 'Living room TV', owner_label: null, platform: 'tvos', device_model: 'AppleTV14,1', is_connected: true, is_guest: false },
	{ ...base, id: 'c-owner-b', name: 'iPhone Owner B', owner_label: 'Owner B', platform: 'ios', device_model: 'iPhone17,1', is_connected: false, is_guest: false, remote_allowed: true, tags: ['kinder'] },
	{ ...base, id: 'c-guest', name: 'Gast Visitor', owner_label: null, platform: 'ios', device_model: 'iPhone16,2', is_connected: true, is_guest: true, expires_at: new Date(Date.now() + 30 * 3600000).toISOString(), tags: ['gast'] },
	{ ...base, id: 'c-old', name: 'Altes iPad', owner_label: null, platform: 'ipados', is_connected: false, is_guest: false, pending_deletion: true, is_active: false },
];
const TEMPLATES = [
	{ id: 'tpl-panel', name: 'Wandpanel', description: '', platform: 'ios', settings: { kiosk_mode: true } },
	{ id: 'tpl-tv', name: 'Fernseher', description: '', platform: 'tvos', settings: { kiosk_mode: true } },
];
const TAGS = ['eg', 'gast', 'kinder', 'area:a-1', '*'];

/** The hub's planning, simplified: enough for the UI paths. */
function plan(body) {
	const results = body.client_ids.map((id) => {
		const c = DEVICES.find((d) => d.id === id);
		const r = { client_id: id, name: c?.name ?? null, status: 'unchanged', changes: [], skipped: [], warnings: [] };
		if (!c || c.pending_deletion) {
			r.status = 'skipped';
			r.skipped = [{ field: '*', reason: c ? 'pending_deletion' : 'not_found' }];
			return r;
		}
		if (body.template_action === 'set') {
			const tpl = TEMPLATES.find((x) => x.id === body.template_id);
			const dev = c.platform === 'tvos' ? 'tvos' : 'ios';
			if (tpl.platform && tpl.platform !== dev) r.skipped.push({ field: 'template', reason: 'template_platform' });
			else r.changes.push('template');
		} else if (body.template_action === 'clear') r.changes.push('template');
		if (body.tags_add?.length || body.tags_remove?.length) {
			const after = c.tags.filter((x) => !(body.tags_remove || []).includes(x)).concat((body.tags_add || []).filter((x) => !c.tags.includes(x)));
			if (JSON.stringify(after) !== JSON.stringify(c.tags)) {
				r.changes.push('tags');
				if (!after.length) r.warnings.push(c.is_guest ? 'guest_sees_no_areas' : 'member_sees_all_areas');
			}
		}
		for (const [field, value] of [['remote_allowed', body.remote_allowed], ['can_invite_guests', body.can_invite_guests]]) {
			if (value === undefined) continue;
			if (c.is_guest) {
				if (value) r.skipped.push({ field, reason: 'guest' });
			} else if (!!c[field] !== value) r.changes.push(field);
		}
		if (body.kiosk_mode !== undefined) {
			if (c.is_guest) {
				if (body.kiosk_mode !== 'clear') r.skipped.push({ field: 'kiosk_mode', reason: 'guest' });
			} else {
				r.changes.push('kiosk_mode');
				if (body.kiosk_mode === 'on' && c.platform === 'ios') r.warnings.push('kiosk_ignored_on_iphone');
			}
		}
		r.status = r.changes.length ? 'changed' : r.skipped.length ? 'skipped' : 'unchanged';
		return r;
	});
	const count = (s) => results.filter((r) => r.status === s).length;
	const fields = ['template', 'tags', 'remote_allowed', 'can_invite_guests', 'kiosk_mode'].filter((f) =>
		f === 'template' ? body.template_action : f === 'tags' ? body.tags_add || body.tags_remove : body[f] !== undefined,
	);
	return { dry_run: body.dry_run, fields, requested: results.length, changed: count('changed'), unchanged: count('unchanged'), skipped: count('skipped'), results };
}

const AUDIT = [
	{ id: 6, ts: ago(2), event: 'admin_action', category: 'action', level: 'info', action: 'clients_bulk_update', details: 'fields=remote_allowed+kiosk_mode requested=4 changed=3 unchanged=0 skipped=1 remote=on kiosk=on ids=c-ipad,c-tv,c-owner-b', ip: '172.30.32.2' },
	{ id: 5, ts: ago(2), event: 'admin_access', category: 'change', level: 'info', action: 'POST /api/pairing/clients/bulk', auth: 'ingress', user: 'Owner A', ip: '172.30.32.2' },
	{ id: 4, ts: ago(11), event: 'admin_action', category: 'action', level: 'info', action: 'guest_defaults', details: 'all_areas=False tags=1 areas=0 template=- hours=24', ip: '172.30.32.2' },
	{ id: 3, ts: ago(22), event: 'admin_access', category: 'change', level: 'info', action: 'PATCH /api/pairing/clients/c-owner-b', auth: 'session', user: 'owner-a', ip: '198.51.100.213' },
	{ id: 2, ts: ago(37), event: 'auth_failure', category: 'security', level: 'warning', action: 'session', details: 'bad_password', ip: '198.51.100.99' },
	{ id: 1, ts: ago(47), event: 'pairing_failure', category: 'security', level: 'warning', details: 'wrong_pin', ip: '198.51.100.120' },
];

async function hub(page, opts = {}) {
	const s = { bulk: [], requests: [], audit: [] };
	if (opts.lang) await page.addInitScript((l) => localStorage.setItem('hub.lang', l), opts.lang);
	await setupApiMocks(page, {
		clients: opts.clients || DEVICES,
		requests: s.requests,
		handler: ({ path, method, request, url, json }) => {
			if (path === '/pairing/clients/bulk' && method === 'POST') {
				const body = request.postDataJSON();
				s.bulk.push(body);
				if (opts.bulkError) return json({ detail: opts.bulkError }, 422);
				return json(plan(body));
			}
			if (path === '/config-templates') return json(opts.templates ?? TEMPLATES);
			if (path === '/tags') return json({ tags: opts.tags ?? TAGS });
			if (path === '/admin/audit') {
				const cats = url.searchParams.getAll('category');
				s.audit.push(cats);
				return json({ max_entries: 500, entries: cats.length ? AUDIT.filter((e) => cats.includes(e.category)) : AUDIT });
			}
			if (path === '/areas/order' && method === 'PUT') return json({ status: 'ok', moved: 2 });
			return undefined;
		},
	});
	return s;
}

const dialog = (page) => page.getByTestId('bulk-dialog');

async function selectDevices(page, ids) {
	for (const id of ids) await page.getByTestId(`select-${id}`).check();
}

test.describe('areas and audit', () => {
	test('reordering areas stores the whole order in one request', async ({ page }) => {
		const s = await hub(page, { lang: 'en' });
		await page.goto('/areas');
		await page.getByRole('button', { name: 'Move Kitchen up' }).click();
		await expect(page.getByText('Order saved')).toBeVisible();
		const writes = s.requests.filter((r) => !r.startsWith('GET'));
		expect(writes).toEqual(['PUT /areas/order']);
	});

	test('audit log: changes by default, security, all, filter', async ({ page }) => {
		const s = await hub(page, { lang: 'en' });
		await page.goto('/settings?tab=system');
		const card = page.getByTestId('audit-card');
		await expect(card.getByRole('heading', { name: 'Audit log' })).toBeVisible();
		await expect(card.getByTestId('audit-feed').locator('li')).toHaveCount(4);
		await expect(card).toContainText('Devices changed together');
		await expect(card).toContainText('Owner A · Home Assistant · 172.30.32.2');
		expect(s.audit[0]).toEqual(['action', 'change']);
		await card.getByRole('button', { name: 'Security' }).click();
		await expect(card.getByTestId('audit-feed').locator('li')).toHaveCount(2);
		await expect(card).toContainText('Failed sign-in');
		expect(s.audit.at(-1)).toEqual(['security']);
		await card.getByRole('button', { name: 'All', exact: true }).click();
		await expect(card.getByTestId('audit-feed').locator('li')).toHaveCount(6);
		expect(s.audit.at(-1)).toEqual([]);
		const segs = await card.getByTestId('audit-view').getByRole('button').evaluateAll((els) => els.map((e) => [Math.round(e.getBoundingClientRect().width), Math.round(e.getBoundingClientRect().height)]));
		for (const b of segs) expect(b).toEqual(segs[0]);
		await card.getByRole('searchbox', { name: 'Filter audit log' }).fill('c-owner-b');
		await expect(card.getByTestId('audit-feed').locator('li')).toHaveCount(2);
	});
});
