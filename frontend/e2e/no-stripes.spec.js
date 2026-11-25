import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';
import { createLogHub } from './log-hub.js';
import { createProfilesHub, AREAS } from './profiles-hub.js';

/**
 * Design rule: no colored accent stripes on the left edge of cards, rows, notes or
 * banners. Highlighting uses a tinted icon, a tinted card, a status dot or a badge instead.
 * The check looks at every rendered element: a left border wider than 1 px that differs from the
 * right border, or an inset box-shadow with a horizontal offset, is a stripe.
 */
test.use({ timezoneId: 'Europe/Zurich', actionTimeout: 15_000 });
expect.configure({ timeout: 15_000 });

const VIEWPORTS = { desktop: { width: 1440, height: 900 }, phone: { width: 390, height: 844 } };

async function stripes(page) {
	return page.evaluate(() => {
		const out = [];
		for (const el of document.querySelectorAll('body *')) {
			if (!el.getClientRects().length) continue;
			const cs = getComputedStyle(el);
			const left = parseFloat(cs.borderLeftWidth) || 0;
			const right = parseFloat(cs.borderRightWidth) || 0;
			const leftStripe = cs.borderLeftStyle !== 'none' && left > 1 && left !== right;
			// Computed form: "rgb(…) 3px 0px 0px 0px inset" – a horizontal inset offset is a stripe.
			const insetStripe = cs.boxShadow
				.split(/,(?![^(]*\))/)
				.some((s) => /inset/.test(s) && /^\s*(rgba?\([^)]*\)\s+)?-?[1-9][\d.]*px\s+0px/.test(s));
			if (leftStripe || insetStripe) out.push(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')} ${left}px ${cs.boxShadow}`);
		}
		return out;
	});
}

async function prepare(page, { lang = 'de', theme = 'light', viewport = 'desktop' }) {
	await page.setViewportSize(VIEWPORTS[viewport]);
	await page.emulateMedia({ colorScheme: theme });
	await page.addInitScript(([l, th]) => {
		localStorage.setItem('hub.lang', l);
		localStorage.setItem('hub.theme', th);
	}, [lang, theme]);
}

const SCENES = [
	['de', 'light', 'desktop'],
	['de', 'dark', 'desktop'],
	['de', 'light', 'phone'],
	['de', 'dark', 'phone'],
	['en', 'light', 'desktop'],
];

for (const [lang, theme, viewport] of SCENES) {
	test(`log timeline without stripes (${lang}, ${theme}, ${viewport})`, async ({ page }) => {
		await prepare(page, { lang, theme, viewport });
		await setupApiMocks(page, { handler: createLogHub().handler });
		await page.goto('/log');
		const alarm = page.getByTestId('log-entry').filter({ hasText: /3 (abgelehnte|denied)/ });
		await expect(alarm).toBeVisible();
		await expect(alarm).toHaveClass(/new/);
		await expect(alarm.locator('.tl-new')).toBeVisible();
		// The alarm type is named once, the profile is never cut.
		const text = (await alarm.innerText()).replace(/\s+/g, ' ');
		expect(text.split(lang === 'de' ? 'Abgelehnte Versuche' : 'Denied attempts').length - 1).toBeLessThanOrEqual(1);
		const cut = await page.locator('.tl-item .tl-sub, .tl-item .tl-line').evaluateAll((els) =>
			els.filter((e) => e.scrollWidth > e.clientWidth + 0.5).map((e) => e.textContent),
		);
		expect(cut).toEqual([]);
		expect(await stripes(page)).toEqual([]);
	});

	test(`device rules without stripes (${lang}, ${theme}, ${viewport})`, async ({ page }) => {
		await prepare(page, { lang, theme, viewport });
		const hub = createProfilesHub({
			overrides: {
				'c-owner-a': [
					{ rule: 'camera_live', value: false, user_choice: false, expires_at: null, note: null, created_by: 'Owner A', created_at: new Date().toISOString() },
				],
			},
		});
		await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
		await page.goto('/clients/c-owner-a');
		const row = page.getByTestId('eff-camera_live');
		await expect(row).toBeVisible();
		await row.scrollIntoViewIfNeeded();
		await expect(row).toHaveClass(/adj/);
		expect(await stripes(page)).toEqual([]);
	});
}
