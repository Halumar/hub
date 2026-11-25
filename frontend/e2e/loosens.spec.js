import { readFileSync } from 'node:fs';
import { test, expect } from '@playwright/test';

/**
 * loosens() of the admin UI (src/lib/profiles/format.js) against the table the
 * hub checks as well (halumar/tests/test_profiles_api.py): the warning before
 * saving and the hub's preview must agree on every case.
 */
const TABLE = JSON.parse(readFileSync(new URL('./fixtures/loosens-cases.json', import.meta.url), 'utf8')).cases;

test('loosens() agrees with the shared table', async ({ page }) => {
	await page.goto('/');
	const got = await page.evaluate(async (cases) => {
		const { loosens } = await import('/src/lib/profiles/format.js');
		return cases.map((c) => loosens({ key: c.rule, security: 'sensitive', order: null }, c.before, c.after));
	}, TABLE);
	const wrong = TABLE.filter((c, i) => got[i] !== c.loosens).map((c) => `${c.rule}: ${c.name} (got ${got[TABLE.indexOf(c)]})`);
	expect(wrong).toEqual([]);
});
