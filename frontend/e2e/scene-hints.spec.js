import { test, expect } from '@playwright/test';
import { setupApiMocks, areaDetail } from './helpers.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';
import { BRAND } from '../src/lib/brand.ts';

/**
 * Scenes and scripts with targets the profile may not switch (`scene_hints`,
 * hub services/scene_targets.py): calm hint in the profile editor (with
 * "add area" into the draft), in the save preview, on the device page
 * ("Effective rules") and on the area page. The mock computes the hints for
 * one scene from the areas rule like the hub does.
 */
test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

const SCENE = {
	entity_id: 'scene.guten_morgen',
	name: 'Guten Morgen',
	area: 'a-wohnen',
	targets: [
		{ entity_id: 'light.kitchen_lights', name: 'Küchenlicht', area: 'a-kueche' },
		{ entity_id: 'light.office', name: 'Bürolampe', area: 'a-buero' },
		{ entity_id: 'cover.living_room_window', name: 'Rollladen', area: 'a-wohnen' },
	],
};

function inRule(rule, areaId) {
	if (!rule) return false;
	if (rule.mode === 'all') return true;
	if ((rule.area_ids || []).includes(areaId)) return true;
	const tags = AREAS.find((a) => a.id === areaId)?.tags || [];
	return (rule.tags || []).some((t) => tags.includes(t));
}

let extraTargets = [];

function hintsFor(rule) {
	if (!inRule(rule, SCENE.area)) return [];
	const targets = [...SCENE.targets, ...extraTargets]
		.filter((t) => t.reason || !t.area || !inRule(rule, t.area))
		.map((t) => ({ entity_id: t.entity_id, name: t.name, reason: t.reason || 'outside_areas', area_ids: t.area ? [t.area] : [] }));
	return targets.length ? [{ entity_id: SCENE.entity_id, name: SCENE.name, area_ids: [SCENE.area], targets_unknown: false, targets }] : [];
}

async function boot(page, lang = 'de', extra = []) {
	extraTargets = extra;
	const hub = createProfilesHub({
		extra: (state) => {
			const guest = state.profiles.find((p) => p.id === 'p-guest');
			guest.rules.areas = { mode: 'selected', area_ids: ['a-wohnen'], tags: [], own_area: true };
		},
	});
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
	const handler = async (ctx) => {
		const { path, method, url, request, json } = ctx;
		if (path === '/areas/a-wohnen' && method === 'GET') return json(areaDetail('a-wohnen', { ...AREAS[0] }));
		const profile = path.match(/^\/profiles\/([^/]+)$/);
		const effective = path.match(/^\/pairing\/clients\/([^/]+)\/(effective|overrides\/[^/]+|own-area)$/);
		if (!profile && !effective) return hub.handler(ctx);
		let out = null;
		const capture = (body, status = 200) => ((out = { body, status }), true);
		await hub.handler({ ...ctx, json: capture });
		if (!out) return json({});
		const body = out.body;
		if (profile && method === 'PATCH' && url.searchParams.get('dry_run') === 'true') {
			const draft = request.postDataJSON()?.rules?.areas;
			const saved = (await fetchProfile(hub, ctx, profile[1])).rules.areas;
			body.scene_hints = hintsFor(draft || saved);
		} else if (profile && body?.rules) {
			body.scene_hints = hintsFor(body.rules.areas);
		} else if (effective && body?.rules?.areas) {
			body.scene_hints = hintsFor(body.rules.areas.value);
		}
		return json(body, out.status);
	};
	await setupApiMocks(page, { handler, areas: AREAS });
	return { hub, errors };
}

async function fetchProfile(hub, ctx, id) {
	let out = null;
	await hub.handler({ ...ctx, path: `/profiles/${id}`, method: 'GET', json: (b) => ((out = b), true) });
	return out;
}

async function equalSizes(locator) {
	const boxes = await locator.evaluateAll((els) => els.filter((e) => e.offsetParent !== null).map((e) => e.getBoundingClientRect()).map((r) => ({ w: r.width, h: r.height })));
	expect(boxes.length).toBeGreaterThan(1);
	for (const b of boxes) {
		expect(Math.abs(b.w - boxes[0].w)).toBeLessThanOrEqual(1.5);
		expect(Math.abs(b.h - boxes[0].h)).toBeLessThanOrEqual(1.5);
	}
}

test('profile editor: hint, add the area to the draft, save preview', async ({ page }) => {
	const { hub, errors } = await boot(page);
	await page.goto('/profiles/p-guest');
	const hints = page.getByTestId('scene-hints');
	await expect(hints).toBeVisible({ timeout: 15_000 });
	await expect(hints.getByTestId('scene-hints-text')).toHaveText('„Guten Morgen“ schaltet „Küchenlicht“, „Bürolampe“ – liegen nicht in den Räumen dieses Profils. Die Szene wird für diese Geräte abgelehnt.');
	const adds = hints.getByTestId('scene-hints-add');
	await expect(adds).toHaveText(['„Küche“ ergänzen', '„Büro“ ergänzen']);
	await equalSizes(adds);
	await adds.first().click();
	await expect(hints.getByTestId('scene-hints-text')).toHaveText('„Guten Morgen“ schaltet „Bürolampe“ – liegt nicht in den Räumen dieses Profils. Die Szene wird für diese Geräte abgelehnt.');
	await expect(page.getByTestId('save-bar')).toBeVisible();
	await page.getByTestId('save-preview').click();
	const dlg = page.getByTestId('save-preview-dialog');
	await expect(dlg.getByTestId('preview-scene-hints')).toContainText('Nach dem Speichern');
	await expect(dlg.getByTestId('preview-scene-hints')).toContainText('„Bürolampe“ – liegt nicht in den Räumen dieses Profils');
	const patch = hub.requests.find(([m, p]) => m === 'PATCH' && p.includes('dry_run'));
	expect(patch[2].rules.areas.area_ids).toEqual(['a-wohnen', 'a-kueche']);
	expect(errors).toEqual([]);
});

test('profile editor in English; no hint when every target is in the profile', async ({ page }) => {
	const { errors } = await boot(page, 'en');
	await page.goto('/profiles/p-guest');
	const hints = page.getByTestId('scene-hints');
	await expect(hints).toContainText('Scenes and scripts with devices outside');
	await expect(hints.getByTestId('scene-hints-text')).toHaveText('“Guten Morgen” switches “Küchenlicht”, “Bürolampe” – not in the rooms of this profile. The scene is refused for these devices.');
	await page.goto('/profiles/p-adult');
	await expect(page.getByTestId('profile-title')).toBeVisible();
	await expect(page.getByTestId('scene-hints')).toHaveCount(0);
	expect(errors).toEqual([]);
});

test('device page: hint in “Effective rules” with a link into the profile', async ({ page }) => {
	const { errors } = await boot(page);
	await page.goto('/clients/c-visitor');
	const hints = page.getByTestId('device-scene-hints');
	await expect(hints).toBeVisible({ timeout: 15_000 });
	await expect(hints.getByTestId('device-scene-hints-text')).toHaveText('„Guten Morgen“ schaltet „Küchenlicht“, „Bürolampe“ – liegen nicht in den Räumen dieses Geräts. Die Szene wird für dieses Gerät abgelehnt.');
	const links = hints.getByTestId('device-scene-hints-add');
	await expect(links.first()).toHaveAttribute('href', /\/profiles\/p-guest#r-areas$/);
	await expect(links).toHaveText(['„Küche“ ergänzen', '„Büro“ ergänzen']);
	await expect(links.first()).toHaveAttribute('aria-label', '„Küche“ im Profil ergänzen');
	await equalSizes(links);
	expect(errors).toEqual([]);
});

test('area page: hints per profile for scenes placed here', async ({ page }) => {
	const { errors } = await boot(page);
	await page.goto('/areas/a-wohnen');
	const box = page.getByTestId('area-scene-hints');
	await expect(box).toBeVisible({ timeout: 15_000 });
	const guest = page.getByTestId('area-scene-hints-p-guest');
	await expect(guest).toContainText('Profil „Gast“');
	await expect(guest.getByTestId('area-scene-hints-p-guest-row')).toContainText('„Guten Morgen“ schaltet „Küchenlicht“, „Bürolampe“');
	await expect(guest.getByTestId('area-scene-hints-p-guest-add').first()).toHaveAttribute('href', /\/profiles\/p-guest#r-areas$/);
	// Profiles with all areas have no hint.
	await expect(page.getByTestId('area-scene-hints-p-adult')).toHaveCount(0);
	expect(errors).toEqual([]);
});

test('phone width: hint without horizontal scroll', async ({ page }) => {
	await page.setViewportSize({ width: 390, height: 844 });
	await boot(page);
	await page.goto('/profiles/p-guest');
	await expect(page.getByTestId('scene-hints')).toBeVisible({ timeout: 15_000 });
	const cut = await page.getByTestId('scene-hints-add').evaluateAll((els) => els.flatMap((e) => [...e.querySelectorAll('.truncate')]).filter((x) => x.scrollWidth > x.clientWidth + 0.5).map((x) => x.textContent));
	expect(cut).toEqual([]);
	const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
	expect(over).toBeLessThanOrEqual(0);
	await equalSizes(page.getByTestId('scene-hints-add'));
});

test('target in no area: link to the area of the scene; other reasons without action', async ({ page }) => {
	const { errors } = await boot(page, 'de', [
		{ entity_id: 'switch.pump', name: 'Pumpe' },
		{ entity_id: 'lock.front_door', name: 'Haustür', area: 'a-wohnen', reason: 'kids_blocked' },
		{ entity_id: 'counter.visits', name: 'Besuche', area: 'a-wohnen', reason: 'not_controllable' },
	]);
	await page.goto('/profiles/p-guest');
	const hints = page.getByTestId('scene-hints');
	await expect(hints.getByTestId('scene-hints-text')).toHaveText([
		'„Guten Morgen“ schaltet „Küchenlicht“, „Bürolampe“, „Pumpe“ – liegen nicht in den Räumen dieses Profils. Die Szene wird für diese Geräte abgelehnt.',
		`„Guten Morgen“ schaltet „Besuche“ – kann ${BRAND.name} nicht schalten. Die Szene wird für diese Geräte abgelehnt.`,
		'„Guten Morgen“ schaltet „Haustür“ – ist für Kinder gesperrt. Die Szene wird für diese Geräte abgelehnt.',
	]);
	const to = hints.getByTestId('scene-hints-to-area');
	await expect(to).toHaveText('Zu „Wohnzimmer“');
	await expect(to).toHaveAttribute('href', /\/areas\/a-wohnen$/);
	await expect(to).toHaveAttribute('aria-label', 'Gerät in „Wohnzimmer“ hinzufügen');
	await equalSizes(hints.locator('.sact .btn'));
	expect(errors).toEqual([]);
});
