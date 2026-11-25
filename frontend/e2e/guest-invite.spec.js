import { test, expect } from '@playwright/test';
import { setupApiMocks, clientDetail } from './helpers.js';

/**
 * Members invite guests from the app. Admin UI: the per-device right
 * "Invite guests", "Invited by" on guests, the default for invitations
 * (areas incl. single areas, duration, app defaults) and the open
 * invitations of member devices (live, withdraw).
 */

const H = 3600_000;
const iso = (ms) => new Date(Date.now() + ms).toISOString();

const AREAS = [
	{ id: 'a-wohnen', name: 'Wohnzimmer', is_active: true, tags: ['gast'] },
	{ id: 'a-kueche', name: 'Küche', is_active: true, tags: [] },
	{ id: 'a-buero', name: 'Büro', is_active: true, tags: ['og'] },
];

const MEMBER = clientDetail({ id: 'm-owner-b', name: 'iPhone Owner B', platform: 'ios', device_model: 'iPhone18,1', can_invite_guests: false });
const GUEST = clientDetail({
	id: 'g-sitter',
	name: 'Sitters iPhone',
	is_guest: true,
	tags: ['area:a-kueche'],
	expires_at: iso(20 * H),
	created_at: iso(-4 * H),
	can_invite_guests: false,
	set_up_by: { client_id: 'm-owner-b', name: 'iPhone Owner B', method: 'invite' },
});

const DEFAULTS = { all_areas: false, tags: ['gast'], area_ids: ['a-kueche'], template_id: null, expires_hours: 24 };
const INVITE = { id: '0e9d8c7b-6a59-4483-9271-605f4e3d2c1b', invited_by: { client_id: 'm-owner-b', name: 'iPhone Owner B' }, created_at: iso(-60_000), expires_at: iso(240_000) };

function handler({ puts, patches, deletes, invites = () => [] }) {
	return ({ path, method, request, json, fulfill }) => {
		if (path === '/areas' && method === 'GET') return json(AREAS);
		if (path === '/tags') return json({ tags: ['gast', 'og'] });
		if (path === '/config-templates' && method === 'GET') return json([{ id: 't-gast', name: 'Gast' }]);
		if (path === '/settings/guest-defaults' && method === 'GET') return json(DEFAULTS);
		if (path === '/settings/guest-defaults' && method === 'PUT') {
			const body = request.postDataJSON();
			puts?.push(body);
			return json(body);
		}
		if (path === '/pairing/guest-invites' && method === 'GET') return json({ invites: invites(), expires_in: 300 });
		if (path.startsWith('/pairing/guest-invites/') && method === 'DELETE') {
			deletes?.push(path);
			return fulfill({ status: 204, body: '' });
		}
		for (const c of [MEMBER, GUEST]) {
			if (path === `/pairing/clients/${c.id}` && method === 'GET') return json(c);
			if (path === `/pairing/clients/${c.id}` && method === 'PATCH') {
				const body = request.postDataJSON();
				patches?.push(body);
				return json({ ...c, ...body });
			}
		}
		return undefined;
	};
}

test('guests page: open invitations of members appear live and can be withdrawn', async ({ page }) => {
	let socket;
	let invites = [];
	const deletes = [];
	await setupApiMocks(page, {
		clients: [MEMBER],
		ws: (ws) => {
			socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
		handler: handler({ deletes, invites: () => invites }),
	});
	await page.goto('/guests');
	const box = page.getByTestId('guest-invites');
	await expect(box.getByTestId('guest-invites-empty')).toBeVisible();
	await expect.poll(() => !!socket).toBe(true);
	invites = [INVITE];
	socket.send(JSON.stringify({ type: 'guest_invite_opened', data: { id: INVITE.id, invited_by: INVITE.invited_by, expires_at: INVITE.expires_at } }));
	const row = box.getByTestId('guest-invite');
	await expect(row).toContainText('From iPhone Owner B');
	await expect(row).not.toContainText(/\d{4}-\d{4}/);
	await row.getByRole('button', { name: 'Withdraw the invitation of iPhone Owner B' }).click();
	await expect.poll(() => deletes).toEqual([`/pairing/guest-invites/${INVITE.id}`]);
	invites = [];
	socket.send(JSON.stringify({ type: 'guest_invite_closed', data: { id: INVITE.id, result: 'revoked' } }));
	await expect(box.getByTestId('guest-invites-empty')).toBeVisible();
});

test('pairing dialog: closes only when its own PIN was used, not for an invited guest', async ({ page }) => {
	let socket;
	let active = true;
	await setupApiMocks(page, {
		clients: [MEMBER, GUEST],
		ws: (ws) => {
			socket = ws;
			ws.send(JSON.stringify({ type: 'auth_ok', client_id: 'ingress' }));
		},
		handler: (ctx) => {
			if (ctx.path === '/pairing/generate' && ctx.method === 'POST') {
				return ctx.json({ pin: '1234-5678', expires_in: 300, expires_at: '', url: 'http://h:8099', deep_link: 'halumar://pair?pin=1234-5678' });
			}
			if (ctx.path === '/pairing/status') {
				return ctx.json(active ? { pairing_active: true, pin: '****-****', remaining_seconds: 290 } : { pairing_active: false });
			}
			return handler({})(ctx);
		},
	});
	await page.goto('/clients');
	await page.getByTestId('open-pairing').click();
	const dialog = page.getByRole('dialog', { name: 'Pair device' });
	await dialog.getByTestId('pairing-generate').click();
	await expect(dialog.getByText('1234-5678')).toBeVisible();
	await expect.poll(() => !!socket).toBe(true);

	// A guest invited by a member pairs meanwhile: the admin PIN is still active.
	socket.send(JSON.stringify({ type: 'client_paired', data: { client_id: GUEST.id, client_name: GUEST.name, is_guest: true } }));
	await page.waitForTimeout(300);
	await expect(dialog).toBeVisible();
	await expect(page).toHaveURL(/\/clients$/);

	// The device of this PIN pairs: the PIN is used up, the dialog opens its page.
	active = false;
	socket.send(JSON.stringify({ type: 'client_paired', data: { client_id: MEMBER.id, client_name: MEMBER.name, is_guest: false } }));
	await expect(dialog).toBeHidden();
	await expect(page).toHaveURL(new RegExp(`/clients/${MEMBER.id}$`));
});
