import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { BRAND } from '../src/lib/brand.ts';

const INGRESS = '/api/hassio_ingress/e2etoken';

test.describe('Admin page only through Home Assistant', () => {
	test('direct access off: a neutral page with a link to Home Assistant, no login form', async ({ page }) => {
		await setupApiMocks(page, {
			session: { authenticated: false, direct_access: false, ha_url: 'http://192.0.2.10:8123' },
		});
		await page.goto('/');
		const box = page.getByTestId('ingress-only');
		await expect(box).toContainText('The admin page is only available through Home Assistant.');
		await expect(box).toContainText(`choose ${BRAND.name} in the sidebar`);
		await expect(box.getByRole('link', { name: 'Open Home Assistant' })).toHaveAttribute('href', 'http://192.0.2.10:8123/');
		await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
		await expect(page.getByLabel('Password')).toHaveCount(0);
	});

	test('without a known address there is no link', async ({ page }) => {
		await setupApiMocks(page, { session: { authenticated: false, direct_access: false, ha_url: null } });
		await page.goto('/settings');
		await expect(page.getByTestId('ingress-only')).toBeVisible();
		await expect(page.getByRole('link', { name: 'Open Home Assistant' })).toHaveCount(0);
	});

	test('an unsafe address is not linked', async ({ page }) => {
		await setupApiMocks(page, { session: { authenticated: false, direct_access: false, ha_url: 'javascript:alert(1)' } });
		await page.goto('/');
		await expect(page.getByTestId('ingress-only')).toBeVisible();
		await expect(page.getByRole('link', { name: 'Open Home Assistant' })).toHaveCount(0);
	});

	test('German texts', async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('hub.lang', 'de'));
		await setupApiMocks(page, { session: { authenticated: false, direct_access: false, ha_url: 'http://192.0.2.10:8123' } });
		await page.goto('/');
		const box = page.getByTestId('ingress-only');
		await expect(box).toContainText('Die Admin-Seite ist nur über Home Assistant erreichbar.');
		await expect(box.getByRole('link', { name: 'Home Assistant öffnen' })).toBeVisible();
	});

	test('direct access switched off meanwhile: the login answer leads to the neutral page', async ({ page }) => {
		let off = false;
		await setupApiMocks(page, {
			handler: ({ path, method, json }) => {
				if (path === '/auth/session') return json(off ? { authenticated: false, direct_access: false, ha_url: null } : { authenticated: false, direct_access: true });
				if (path === '/auth/login' && method === 'POST') {
					off = true;
					return json({ detail: 'The admin page is only available through Home Assistant.', reason: 'admin_ingress_only' }, 403);
				}
				return undefined;
			},
		});
		await page.goto('/');
		await page.getByLabel('Username').fill('admin');
		await page.getByLabel('Password').fill('secret');
		await page.getByRole('button', { name: 'Sign in' }).click();
		await expect(page.getByTestId('ingress-only')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
	});
});

test.describe('Setting: admin page directly at home', () => {
	test('off by default; switching it on in Home Assistant saves it', async ({ page }) => {
		const puts = [];
		await setupApiMocks(page, {
			ingress: INGRESS,
			handler: ({ path, method, request, json }) => {
				if (path !== '/settings/admin-direct-access') return undefined;
				if (method === 'PUT') {
					puts.push(request.postDataJSON());
					return json(request.postDataJSON());
				}
				return json({ admin_direct_access: false });
			},
		});
		await page.goto('/settings?tab=advanced');
		const card = page.getByTestId('admin-access-card');
		const sw = card.getByRole('switch', { name: 'Also open the admin page directly at home' });
		await expect(sw).toHaveAttribute('aria-checked', 'false');
		await expect(sw).toBeEnabled();
		await expect(card).toContainText('Switching off ends every direct sign-in.');
		await sw.click();
		await expect(sw).toHaveAttribute('aria-checked', 'true');
		expect(puts).toEqual([{ admin_direct_access: true }]);
		await expect(page.getByText('Direct access to the admin page enabled')).toBeVisible();
	});

	test('in a direct session the switch is read-only', async ({ page }) => {
		await setupApiMocks(page, {
			session: { authenticated: true, username: 'tester' },
			handler: ({ path, json }) => (path === '/settings/admin-direct-access' ? json({ admin_direct_access: true }) : undefined),
		});
		await page.goto('/settings?tab=advanced');
		const card = page.getByTestId('admin-access-card');
		const sw = card.getByRole('switch', { name: 'Also open the admin page directly at home' });
		await expect(sw).toHaveAttribute('aria-checked', 'true');
		await expect(sw).toBeDisabled();
		await expect(card).toContainText('Can only be changed in Home Assistant.');
	});
});
