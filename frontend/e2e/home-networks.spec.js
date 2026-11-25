import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * Settings › Network lists
 * further networks that count as at home (templates, own ranges, 422 codes,
 * Tailscale hint) and the calm hint when Home Assistant is reached over http.
 *Mocked API.
 */

function hub({ networks = [], haUrls = { ha_url: 'https://x.ui.nabu.casa', ha_internal_url: 'https://ha.local:8123' } } = {}) {
	const state = { networks, puts: [] };
	const handler = async ({ path, method, request, json }) => {
		if (path === '/settings/ha-urls' && method === 'GET') return json(haUrls);
		if (path !== '/settings/home-networks') return undefined;
		if (method === 'GET') return json({ networks: state.networks });
		const body = JSON.parse(request.postData() || '{}');
		state.puts.push(body.networks);
		const bad = body.networks.find((n) => n.cidr.startsWith('8.'));
		if (bad) return json({ detail: { code: 'home_network_public', cidr: bad.cidr, message: 'not private' } }, 422);
		state.networks = body.networks.map((n) => ({ cidr: n.cidr, name: n.name ?? null }));
		return json({ networks: state.networks });
	};
	return { state, handler };
}

async function open(page, lang, mock) {
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	await setupApiMocks(page, { handler: mock.handler });
	await page.goto('/settings?tab=network');
}

for (const [lang, title, hint, add] of [
	['en', 'Other addresses that count as home', 'Mobile networks use 100.64.x.x too', 'Add'],
	['de', 'Weitere Adressen, die als zu Hause gelten', 'Auch Mobilfunk-Netze nutzen 100.64.x.x', 'Hinzufügen'],
]) {
	test(`Tailscale template adds IPv4 and IPv6 with the mobile hint (${lang})`, async ({ page }) => {
		const mock = hub();
		await open(page, lang, mock);
		await expect(page.getByRole('heading', { name: title })).toBeVisible();
		await expect(page.getByTestId('home-networks-empty')).toBeVisible();
		await page.getByTestId('home-network-template-tailscale').click();
		await expect(page.getByTestId('home-networks-tailscale')).toContainText(hint);
		await expect(page.locator('#home-network-cidr')).toHaveValue('100.64.0.0/10');
		await page.getByRole('button', { name: add }).click();
		await expect(page.getByTestId('home-network')).toHaveCount(2);
		expect(mock.state.puts.at(-1)).toEqual([
			{ cidr: '100.64.0.0/10', name: 'Tailscale' },
			{ cidr: 'fd7a:115c:a1e0::/48', name: 'Tailscale' },
		]);
		await expect(page.getByTestId('home-networks-shared')).toContainText(hint);
	});
}

test('templates are equally wide', async ({ page }) => {
	for (const [lang, width] of [['de', 1280], ['de', 420], ['en', 1280]]) {
		await page.setViewportSize({ width, height: 900 });
		await open(page, lang, hub());
		const boxes = [];
		for (const id of ['tailscale', 'wireguard', 'zerotier', 'custom']) {
			boxes.push(await page.getByTestId(`home-network-template-${id}`).boundingBox());
		}
		const widths = boxes.map((b) => Math.round(b.width));
		const heights = boxes.map((b) => Math.round(b.height));
		expect(Math.max(...widths) - Math.min(...widths)).toBeLessThanOrEqual(1);
		expect(Math.max(...heights) - Math.min(...heights)).toBeLessThanOrEqual(1);
	}
});

test('WireGuard opens the input with an example, own name and a removal', async ({ page }) => {
	const mock = hub({ networks: [{ cidr: '172.16.77.0/24', name: null }] });
	await open(page, 'en', mock);
	await expect(page.getByTestId('home-network')).toHaveCount(1);
	await page.getByTestId('home-network-template-wireguard').click();
	await expect(page.locator('#home-network-cidr')).toHaveAttribute('placeholder', '10.8.0.0/24');
	await expect(page.locator('#home-network-name')).toHaveValue('WireGuard');
	await page.locator('#home-network-cidr').fill('10.8.0.0/24');
	await page.locator('#home-network-name').fill('Office VPN');
	await page.getByTestId('home-network-add').click();
	await expect(page.getByTestId('home-network')).toHaveCount(2);
	expect(mock.state.puts.at(-1)).toEqual([
		{ cidr: '172.16.77.0/24', name: null },
		{ cidr: '10.8.0.0/24', name: 'Office VPN' },
	]);
	await expect(page.getByTestId('home-networks-shared')).toHaveCount(0);
	await page.getByRole('button', { name: 'Remove 172.16.77.0/24' }).click();
	await expect(page.getByTestId('home-network')).toHaveCount(1);
});

for (const [lang, text] of [
	['en', '8.8.8.0/24 is not a private range'],
	['de', '8.8.8.0/24 ist kein privater Bereich'],
]) {
	test(`a public range shows the hub's reason (${lang})`, async ({ page }) => {
		const mock = hub();
		await open(page, lang, mock);
		await page.getByTestId('home-network-template-custom').click();
		await page.locator('#home-network-cidr').fill('8.8.8.0/24');
		await page.getByTestId('home-network-add').click();
		await expect(page.getByTestId('home-networks-error')).toContainText(text);
		await expect(page.getByTestId('home-network')).toHaveCount(0);
	});
}

test('not a CIDR is refused before sending', async ({ page }) => {
	const mock = hub();
	await open(page, 'en', mock);
	await page.locator('#home-network-cidr').fill('tailscale');
	await page.getByTestId('home-network-add').click();
	await expect(page.getByTestId('home-networks-error')).toContainText('not a range in CIDR notation');
	expect(mock.state.puts).toHaveLength(0);
});

test('ten ranges: adding is closed', async ({ page }) => {
	const networks = Array.from({ length: 10 }, (_, i) => ({ cidr: `10.${i}.0.0/16`, name: null }));
	await open(page, 'en', hub({ networks }));
	await expect(page.getByTestId('home-network')).toHaveCount(10);
	await expect(page.getByTestId('home-network-add')).toBeDisabled();
	await expect(page.getByTestId('home-network-template-tailscale')).toBeDisabled();
});

for (const [lang, text, link] of [
	['en', 'Home Assistant is reachable unencrypted on the home network (http). Recommended: set up https.', 'How to'],
	['de', 'Home Assistant ist im Heimnetz unverschlüsselt erreichbar (http). Empfehlung: https einrichten.', 'Anleitung'],
]) {
	test(`Http Home Assistant gets a calm hint with the guide (${lang})`, async ({ page }) => {
		const mock = hub({ haUrls: { ha_url: null, ha_internal_url: null, ha_internal_url_detected: 'http://198.51.100.178' } });
		await open(page, lang, mock);
		const hint = page.getByTestId('ha-cleartext-hint');
		await expect(hint).toContainText(text);
		await expect(hint.getByRole('link', { name: link })).toHaveAttribute('href', 'https://halumar.com/docs/connection/');
		await page.goto('/');
		await expect(page.getByTestId('ha-cleartext-hint')).toContainText(text);
	});
}

test('Https Home Assistant shows no hint', async ({ page }) => {
	await open(page, 'en', hub());
	await expect(page.getByRole('heading', { name: 'Home Assistant URLs' })).toBeVisible();
	await expect(page.getByTestId('ha-cleartext-hint')).toHaveCount(0);
	await page.goto('/');
	await expect(page.getByTestId('kpi-online')).toBeVisible();
	await expect(page.getByTestId('ha-cleartext-hint')).toHaveCount(0);
});
