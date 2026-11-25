import { test, expect } from '@playwright/test';
import { setupApiMocks } from './helpers.js';

/**
 * Times the admin page proposes are local to the browser in any time zone,
 * also on the night of a daylight saving change: "Tomorrow 08:00" is the next
 * calendar day at 08:00, never two days ahead.
 */
const CASES = [
	// zone, "now" (the evening before the clocks change), expected "Tomorrow 08:00"
	['America/Los_Angeles', '2027-03-14T07:30:00Z', '2027-03-14T08:00'], // Mar 13 23:30 PST, spring forward on Mar 14
	['America/Los_Angeles', '2026-11-01T06:30:00Z', '2026-11-01T08:00'], // Oct 31 23:30 PDT, fall back on Nov 1
	['Europe/Zurich', '2027-03-27T22:30:00Z', '2027-03-28T08:00'], // Mar 27 23:30 CET, spring forward on Mar 28
	['UTC', '2026-11-18T23:30:00Z', '2026-11-19T08:00'],
];

for (const [zone, now, expected] of CASES) {
	test.describe(`schedule presets in ${zone}`, () => {
		test.use({ timezoneId: zone });
		test(`"Tomorrow 08:00" is the next calendar day (${zone}, ${now})`, async ({ page }) => {
			await page.clock.setFixedTime(new Date(now));
			await setupApiMocks(page);
			await page.goto('/notifications');
			await page.getByRole('button', { name: /Compose/ }).first().click();
			await page.getByRole('radio', { name: 'Scheduled', exact: true }).or(page.getByRole('button', { name: 'Scheduled', exact: true })).first().click();
			await page.getByRole('button', { name: 'Tomorrow 08:00' }).click();
			await expect(page.locator('input[type="datetime-local"]')).toHaveValue(expected);
		});
	});
}
