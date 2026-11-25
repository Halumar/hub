import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E configuration for the Halumar SvelteKit frontend.
 *
 * These tests run against a fully MOCKED API: every test intercepts /api/*
 * with page.route(), so NO live add-on backend is required. This makes the
 * suite safe to run in CI without the Python backend or Home Assistant.
 *
 * The webServer below boots the SvelteKit dev server (vite dev). Dev mode is
 * used instead of build+preview because the static adapter writes its output
 * to a path outside this package (../halumar/rootfs/...), which `vite preview`
 * does not serve. Dev mode serves the same routes and, since the API is mocked
 * at the network layer, behaves identically for these smoke tests.
 *
 * To run against a production build instead, change `command` to
 * `npm run build && npm run preview`, set `port` to 4173, and update baseURL.
 */

// Dedicated, uncommon port to avoid colliding with any other dev server the
// developer may already have running on the default Vite port (5173).
// PW_PORT lets parallel checkouts run their own dev server.
const PORT = Number(process.env.PW_PORT) || 5191;
const BASE_URL = `http://localhost:${PORT}`;

export default defineConfig({
	testDir: './e2e',
	/* Run files in parallel. */
	fullyParallel: true,
	/* Fail the build on CI if test.only is left in the source. */
	forbidOnly: !!process.env.CI,
	retries: process.env.CI ? 1 : 0,
	workers: process.env.CI ? 1 : undefined,
	reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',

	use: {
		baseURL: BASE_URL,
		trace: 'on-first-retry',
		screenshot: 'only-on-failure',
	},

	projects: [
		{
			name: 'chromium',
			use: { ...devices['Desktop Chrome'] },
		},
	],

	webServer: {
		command: 'npm run dev -- --port ' + PORT,
		url: BASE_URL,
		reuseExistingServer: !process.env.CI,
		timeout: 120_000,
	},
});
