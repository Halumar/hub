import { test, expect } from '@playwright/test';
import { BRAND } from '../src/lib/brand.ts';
import { setupApiMocks } from './helpers.js';
import { createProfilesHub, defaultClients, AREAS } from './profiles-hub.js';

test.use({ actionTimeout: 15_000 });
expect.configure({ timeout: 10_000 });

/**
 * "Home Assistant access" (rule control_path: Hub only, Checked, Direct) in the
 * admin UI against the in-memory hub: the rule editor, the locks of the
 * features that need the device's own key, the read-only key that remote
 * access hands out on "Hub only" (editor, adjustment, pairing, preview), TVs
 * and watches that always hold a key, the Pro hint of parental controls and
 * the access level and key on the device list and page.
 */

const ago = (min) => new Date(Date.now() - min * 60_000).toISOString();

function clientOf(fields) {
	return {
		is_active: true,
		is_connected: true,
		access: 'member',
		expires_at: null,
		set_up_by: null,
		owner_label: null,
		own_area_id: null,
		has_remote_pin: false,
		ha_user_id: 'ha-user',
		ha_token_status: 'active',
		ha_token_pending_since: null,
		platform: 'ios',
		app_version: '2026.12.0',
		app_build: '7',
		device_model: 'iPhone18,1',
		protocol_version: 1,
		update_required: false,
		created_at: ago(60 * 24 * 3),
		last_seen: ago(2),
		...fields,
	};
}

/** The default household plus a parent on Direct, a member on Hub only (phone and TV) and a house sitter profile with remote access. */
function household() {
	return {
		clients: [
			...defaultClients(),
			clientOf({ id: 'c-parent', name: 'Parent phone', profile_id: 'p-parent' }),
			clientOf({ id: 'c-hubphone', name: 'Hub phone', platform: 'android', device_model: 'Pixel 9', profile_id: 'p-hub' }),
			clientOf({ id: 'c-tv2', name: 'Kitchen TV', platform: 'android_tv', device_model: 'Chromecast', profile_id: 'p-hub' }),
		],
		extra: (state, { newProfile }) => {
			state.profiles.push(newProfile('p-parent', 'parent'));
			const hub = newProfile('p-hub', 'adult', 'Nur Hub');
			hub.rules.control_path = 'hub';
			hub.rules.remote_access = false;
			hub.rules.setup_devices = { enabled: true, kinds: ['guest', 'tv'] };
			state.profiles.push(hub);
			const sitter = newProfile('p-sitter', 'house_sitter');
			sitter.rules.remote_access = true;
			state.profiles.push(sitter);
		},
	};
}

async function boot(page, opts = household(), lang = 'de') {
	const hub = createProfilesHub(opts);
	await page.addInitScript((l) => localStorage.setItem('hub.lang', l), lang);
	const errors = [];
	page.on('pageerror', (e) => errors.push(String(e)));
	page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
	await setupApiMocks(page, { handler: hub.handler, areas: AREAS });
	return { hub, errors };
}

const writes = (hub, method, prefix) => hub.requests.filter(([m, p]) => m === method && p.startsWith(prefix));
const radios = (page, testid) => page.getByTestId(testid).getByRole('radio');
/** Remote access in the profile editor (a boolean rule: two radios). */
const remote = (page, name) => page.getByTestId('p-remote_access-remote_access').getByRole('radio', { name, exact: true }).click();

test.describe('Rule editor: Home Assistant access', () => {
	test('three levels, strict first, with label and help from the catalog', async ({ page }) => {
		const { errors } = await boot(page);
		await page.goto('/profiles/p-adult');
		const row = page.getByTestId('rule-control_path');
		await expect(row.getByRole('heading')).toHaveText('Zugang zu Home Assistant');
		await expect(row).toContainText('Nur über den Hub: Das Gerät bekommt keinen Home-Assistant-Schlüssel.');
		await expect(radios(page, 'p-control_path-control_path')).toHaveText(['Nur über den Hub', 'Geprüft', 'Direkt']);
		// Adult is on "Checked".
		await expect(page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Geprüft' })).toHaveAttribute('aria-checked', 'true');
		await expect(page.getByTestId('platform-key')).toHaveCount(0);
		expect(errors).toEqual([]);
	});

	test('each level sets the locks and the enforcement markers', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-adult');
		const seg = page.getByTestId('p-control_path-control_path');
		// Checked: Home Assistant enforces operating, widgets and watch are free.
		await expect(page.getByTestId('enf-areas')).toContainText('Bedienen durchgesetzt');
		await expect(page.getByTestId('decided-quick_access')).toHaveCount(0);

		// Direct: the rules apply in the app only.
		await seg.getByRole('radio', { name: 'Direkt' }).click();
		await expect(page.getByTestId('enf-areas')).toContainText('Gilt in');

		// Hub only: the hub enforces; with remote access (Adult has it) widgets and the watch stay free.
		await seg.getByRole('radio', { name: 'Nur über den Hub' }).click();
		await expect(page.getByTestId('enf-areas')).toContainText('Durchgesetzt');
		await expect(page.getByTestId('enf-areas')).not.toContainText('Bedienen');
		await expect(page.getByTestId('decided-quick_access')).toHaveCount(0);
		// The catalog sentence of platform_needs_key with the platforms that never run without a key.
		await expect(page.getByTestId('platform-key')).toContainText('Auf Apple TV, Android TV oder Apple Watch');

		// Hub only without remote access: widgets and the watch need the device's own key.
		await remote(page, 'Aus');
		for (const key of ['quick_access', 'watch']) {
			const lock = page.getByTestId(`decided-${key}`);
			await expect(lock).toHaveAttribute('data-reason', 'boundary_hub_only');
			await expect(lock).toContainText('Braucht einen eigenen Home-Assistant-Schlüssel: mit „Nur über den Hub“ ohne Fernzugriff nicht möglich');
		}
		await expect(page.getByTestId('rule-quick_access')).toContainText('Aus');
		await page.getByTestId('decided-watch').getByRole('button', { name: 'Warum?' }).click();
		await expect(page.getByTestId('why-watch')).toContainText('eine Uhr');
	});

	test('saving "Hub only" shows the lost key in the preview', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles/p-adult');
		await page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Nur über den Hub' }).click();
		await remote(page, 'Aus');
		await page.getByTestId('save-preview').click();
		const dev = page.getByTestId('save-preview-device').filter({ hasText: 'iPhone Owner A' });
		await expect(dev).toContainText('Verliert seinen Home-Assistant-Schlüssel und liest und schaltet nur noch über den Hub.');
		await expect(dev).toContainText('Geprüft');
		await expect(dev).toContainText('Nur über den Hub');
		await page.getByTestId('save-confirm').click();
		await expect(page.getByTestId('save-preview-dialog')).toBeHidden();
		const patch = writes(hub, 'PATCH', '/profiles/p-adult').map((r) => r[2]).at(-1);
		expect(patch.rules).toEqual({ control_path: 'hub', remote_access: false });
	});

	test('a child profile without a stored value shows "Hub only"', async ({ page }) => {
		const opts = household();
		const extra = opts.extra;
		opts.extra = (state, helpers) => {
			extra(state, helpers);
			delete state.profiles.find((p) => p.id === 'p-child').rules.control_path;
		};
		await boot(page, opts);
		await page.goto('/profiles/p-child');
		await expect(page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Nur über den Hub' })).toHaveAttribute('aria-checked', 'true');
		await expect(page.getByTestId('decided-quick_access')).toHaveAttribute('data-reason', 'boundary_hub_only');
		await page.goto('/clients/c-kid');
		await expect(page.getByTestId('eff-control_path')).toContainText('Nur über den Hub');
	});

	test('setting up devices on "Hub only" names what needs a key', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-hub');
		await expect(page.getByTestId('setup-hub')).toContainText('keine Fernseher und keine Uhren');
		await remote(page, 'An');
		await expect(page.getByTestId('setup-hub')).toContainText('auch Fernseher und Uhren einrichten');
	});

	test('guest kinds without a stored value show "Hub only"', async ({ page }) => {
		const opts = household();
		const extra = opts.extra;
		opts.extra = (state, helpers) => {
			extra(state, helpers);
			delete state.profiles.find((p) => p.id === 'p-sitter').rules.control_path;
		};
		await boot(page, opts);
		await page.goto('/profiles/p-sitter');
		await expect(page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Nur über den Hub' })).toHaveAttribute('aria-checked', 'true');
	});

	test('a child may choose "Checked", never "Direct"', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-child');
		const seg = page.getByTestId('p-control_path-control_path');
		await seg.getByRole('radio', { name: 'Geprüft' }).click();
		await expect(page.getByTestId('enf-areas')).toContainText('Bedienen durchgesetzt');
		await seg.getByRole('radio', { name: 'Direkt' }).click();
		await expect(page.getByTestId('rule-control_path')).toContainText('Mit Kindersicherung höchstens: Geprüft');
	});

	test('switching parental controls on proposes "Hub only" as a visible change', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles/p-adult');
		await page.getByTestId('child-protection').click();
		await expect(page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Nur über den Hub' })).toHaveAttribute('aria-checked', 'true');
		await expect(page.getByTestId('child-hub-set')).toContainText('„Zugang zu Home Assistant“ ist auf „Nur über den Hub“ gestellt');
		await page.getByTestId('save-preview').click();
		const dev = page.getByTestId('save-preview-device').filter({ hasText: 'iPhone Owner A' });
		await expect(dev.locator('li').filter({ hasText: 'Zugang zu Home Assistant' })).toContainText('Geprüft');
		await expect(dev.locator('li').filter({ hasText: 'Zugang zu Home Assistant' })).toContainText('Nur über den Hub');
		// Adult has remote access: the device keeps a read-only key, and the editor says so.
		await expect(dev).not.toContainText('Verliert seinen Home-Assistant-Schlüssel');
		await page.keyboard.press('Escape');
		await expect(page.getByTestId('remote-key-warning-profile')).toBeVisible();
		// Off again: the saved path comes back, nothing is left to save.
		await page.getByTestId('child-protection').click();
		await expect(page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Geprüft' })).toHaveAttribute('aria-checked', 'true');
		await expect(page.getByTestId('save-bar')).toHaveCount(0);
		expect(writes(hub, 'PATCH', '/profiles/p-adult')[0][2]).toMatchObject({ child_protection: true, rules: { control_path: 'hub' } });
	});

	test('TV profiles do not offer "Hub only" and say why', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-tv');
		await expect(radios(page, 'p-control_path-control_path')).toHaveText(['Geprüft', 'Direkt']);
		await expect(page.getByTestId('platform-key-tv')).toContainText('Auf Apple TV oder Android TV');
		// A TV on a "Hub only" profile resolves to Checked.
		await page.goto('/clients/c-tv2');
		await expect(page.getByTestId('eff-control_path')).toContainText('Geprüft');
		await expect(page.getByTestId('src-control_path')).toHaveAttribute('data-reason', 'platform_needs_key');
		await expect(page.getByTestId('src-control_path')).toContainText('Auf Android TV');
		// Adjusting a TV never offers "Hub only".
		await page.goto('/clients/c-tv');
		await page.getByTestId('adjust-control_path').click();
		await expect(radios(page, 'adj-control_path')).toHaveText(['Geprüft', 'Direkt']);
	});
});

test.describe('Pro', () => {
	test('parental controls and the kids rules carry the Pro badge and the hint', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-child');
		await expect(page.getByTestId('child-protection-pro-badge')).toHaveText('Pro');
		await expect(page.getByTestId('child-protection-pro')).toHaveText(`Braucht ${BRAND.name} Pro in der App auf dem Gerät des Kindes.`);
		for (const key of ['kids_blocked_types', 'kids_quiet_hours', 'kids_rate_limit', 'kids_rate_limit_all', 'kids_volume_max', 'kids_color_lock', 'kids_requests', 'kids_exceptions', 'kids_watch']) {
			await expect(page.getByTestId(`rule-${key}`).locator('.rname .badge-accent')).toHaveText('Pro');
		}
		// "Hub only" itself never needs Pro.
		await expect(page.getByTestId('rule-control_path').locator('.rname .badge-accent')).toHaveCount(0);
	});

	test('the new-profile dialog names Pro next to parental controls', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles');
		await page.getByTestId('new-profile').click();
		await page.getByTestId('new-empty').click();
		await expect(page.getByTestId('new-child-pro')).toContainText(`Braucht ${BRAND.name} Pro in der App auf dem Gerät des Kindes.`);
	});
});

test.describe('Remote access on "Hub only" hands out a read-only key', () => {
	test('profile editor: the warning sits next to the switch, the preview repeats it and asks first', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles/p-guest');
		await expect(page.getByTestId('remote-key-warning-profile')).toHaveCount(0);
		await remote(page, 'An');
		const warn = page.getByTestId('remote-key-warning-profile');
		await expect(warn).toContainText('Mit „Nur über den Hub“ bekommt');
		await expect(warn).toContainText('jedes Gerät mit diesem Profil mit Fernzugriff einen Home-Assistant-Schlüssel, der nur lesen darf');
		await expect(warn).toContainText('Schalte das nur für Personen ein, denen du vertraust.');
		// The catalog help stays short and complete; the banner says what the key can read.
		await expect(page.getByTestId('help-remote_access')).toHaveText('Steuert Home Assistant auch ausserhalb des Heimnetzes. Mit Kindersicherung gelten die Kinderregeln auch unterwegs. Mit „Nur über den Hub“ bekommt das Gerät dafür einen Home-Assistant-Schlüssel, der nur lesen darf.');
		// Remote access lifts the "Hub only" lock of widgets (guest devices keep their own cap).
		await expect(page.getByTestId('decided-quick_access')).toHaveCount(0);
		await expect(page.getByTestId('rule-quick_access')).toContainText('Auf Gastgeräten höchstens: Aus');
		await page.getByTestId('save-preview').click();
		const dev = page.getByTestId('save-preview-device').filter({ hasText: 'iPhone Visitor' });
		await expect(dev).toContainText('Bekommt einen eigenen Home-Assistant-Schlüssel.');
		await expect(page.getByTestId('save-preview-remote-key-warning')).toContainText('bekommt das Gerät mit Fernzugriff einen Home-Assistant-Schlüssel, der nur lesen darf');
		await expect(page.getByTestId('save-confirm')).toBeDisabled();
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('save-confirm').click();
		await expect(page.getByTestId('save-preview-dialog')).toBeHidden();
		expect(writes(hub, 'PATCH', '/profiles/p-guest').at(-1)[2].rules).toEqual({ remote_access: true });
	});

	test('no warning on "Checked": the key exists already', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-wall');
		await remote(page, 'An');
		await expect(page.getByTestId('remote-key-warning-profile')).toHaveCount(0);
		await page.getByTestId('save-preview').click();
		await expect(page.getByTestId('save-preview-device').first()).toBeVisible();
		await expect(page.getByTestId('save-preview-remote-key-warning')).toHaveCount(0);
	});

	test('device adjustment, also a temporary one', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/clients/c-hubphone');
		await page.getByTestId('adjust-remote_access').click();
		await expect(page.getByTestId('adjust-remote-key-warning')).toHaveCount(0);
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'An' }).click();
		await expect(page.getByTestId('adjust-key-ends')).toHaveCount(0);
		await page.getByTestId('duration-1h').check();
		await expect(page.getByTestId('adjust-key-ends')).toContainText('Danach hat das Gerät wieder keinen Home-Assistant-Schlüssel');
		await expect(page.getByTestId('adjust-remote-key-warning')).toContainText('bekommt das Gerät mit Fernzugriff einen Home-Assistant-Schlüssel, der nur lesen darf');
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('adjust-save').click();
		await expect(page.getByTestId('src-remote_access')).toContainText('Angepasst bis');
		expect(writes(hub, 'PUT', '/pairing/clients/c-hubphone/overrides/remote_access').filter((r) => !r[1].includes('dry_run'))[0][2]).toEqual({ value: true, note: null, expires: '1h' });
		// Widgets and the watch end with the temporary remote access.
		await expect(page.getByTestId('src-quick_access-until')).toContainText('danach Aus');
		await expect(page.getByTestId('src-watch-until')).toContainText('danach Aus');
		// With the temporary remote access the device holds a read-only key.
		await expect(page.getByTestId('ha-key')).toHaveAttribute('data-key', 'read_only');
	});

	test('device adjustment to "Hub only" while remote access is on', async ({ page }) => {
		await boot(page);
		await page.goto('/clients/c-owner-a');
		await page.getByTestId('adjust-control_path').click();
		await expect(radios(page, 'adj-control_path')).toHaveText(['Nur über den Hub', 'Geprüft', 'Direkt']);
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'Nur über den Hub' }).click();
		await expect(page.getByTestId('adjust-remote-key-warning')).toBeVisible();
	});

	test('pairing: a profile with "Hub only" and remote access warns before the PIN', async ({ page }) => {
		await boot(page);
		await page.goto('/guests');
		await page.getByTestId('open-pairing-guest').click();
		await expect(page.getByTestId('pairing-profile-p-guest')).toBeChecked();
		await expect(page.getByTestId('pairing-remote-key-warning')).toHaveCount(0);
		await page.getByTestId('pairing-profile-p-sitter').check();
		await expect(page.getByTestId('pairing-remote')).toContainText('erlaubt laut Profil');
		await expect(page.getByTestId('pairing-remote-key-warning')).toContainText('jeden Zustand in Home Assistant lesen');
		await expect(page.getByTestId('pairing-generate')).toBeVisible();
	});
});

test.describe('Device list and page', () => {
	test('the list shows the access level and the key of every device', async ({ page }) => {
		await boot(page);
		await page.goto('/clients');
		const cases = {
			'c-owner-a': ['checked', 'read_only', 'Geprüft', 'Schlüssel nur lesen'],
			'c-parent': ['direct', 'full', 'Direkt', 'Voller Schlüssel'],
			'c-kid': ['hub', 'none', 'Nur über den Hub', 'Kein Schlüssel'],
			'c-visitor': ['hub', 'none', 'Nur über den Hub', 'Kein Schlüssel'],
			'c-hubphone': ['hub', 'none', 'Nur über den Hub', 'Kein Schlüssel'],
			'c-tv2': ['checked', 'read_only', 'Geprüft', 'Schlüssel nur lesen'],
		};
		await expect(page.getByTestId('device-table').locator('thead')).toContainText('HA-Zugang');
		for (const [id, [level, key, label, keyText]] of Object.entries(cases)) {
			const cell = page.getByTestId(`ha-access-${id}`);
			await expect(cell).toHaveAttribute('data-level', level);
			await expect(cell).toHaveAttribute('data-key', key);
			await expect(cell).toContainText(label);
			await expect(cell).toContainText(keyText);
		}
	});

	test('a key not yet rotated and a key being set up are named as such', async ({ page }) => {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-old', name: 'Old phone', profile_id: 'p-hub', stale_key: true }));
		opts.clients.push(clientOf({ id: 'c-new', name: 'New phone', profile_id: 'p-adult', ha_token_status: 'pending', ha_token_pending_since: ago(1) }));
		await boot(page, opts);
		await page.goto('/clients');
		await expect(page.getByTestId('ha-access-c-old')).toHaveAttribute('data-key', 'revoking');
		await expect(page.getByTestId('ha-access-c-old')).toContainText('Schlüssel wird entzogen');
		await expect(page.getByTestId('ha-access-c-new')).toHaveAttribute('data-key', 'pending');
		await page.goto('/clients/c-old');
		await expect(page.getByTestId('ha-key-hint')).toContainText('bis der Hub den Ersatz erhalten und die Integration das alte Token entfernt hat');
	});

	test('the list shows nothing it does not know', async ({ page }) => {
		const { hub } = await boot(page);
		const handler = hub.handler;
		await page.unrouteAll();
		await setupApiMocks(page, {
			areas: AREAS,
			handler: async (args) => {
				if (args.path === '/pairing/clients' && args.method === 'GET') return args.json(hub.state.clients.map((c) => ({ ...hub.clientOut(c), control_path: undefined })));
				return handler(args);
			},
		});
		await page.goto('/clients');
		await expect(page.getByTestId('ha-access-c-owner-a')).toHaveCount(0);
	});

	test('the device page shows level, key and group', async ({ page }) => {
		await boot(page);
		await page.goto('/clients/c-kid');
		await expect(page.getByTestId('ha-access')).toHaveText('Nur über den Hub');
		await expect(page.getByTestId('ha-key')).toHaveText('Kein Schlüssel');
		await expect(page.getByTestId('ha-key-hint')).toContainText('liest und schaltet nur über den Hub');
		await expect(page.getByTestId('ha-group')).toHaveText('Nur lesen (nie Admin)');
		await page.goto('/clients/c-parent');
		await expect(page.getByTestId('ha-access')).toHaveText('Direkt');
		await expect(page.getByTestId('ha-key')).toHaveText('Voller Schlüssel');
		await expect(page.getByTestId('ha-group')).toHaveText('Benutzer (nie Admin)');
		await page.goto('/clients/c-owner-a');
		await expect(page.getByTestId('ha-key')).toHaveText('Schlüssel nur lesen');
		await expect(page.getByTestId('ha-key-hint')).toContainText('jeden Zustand in Home Assistant lesen');
	});
});

test.describe('English', () => {
	test('rule editor, locks, warnings and the device page in English', async ({ page }) => {
		const { errors } = await boot(page, household(), 'en');
		await page.goto('/profiles/p-hub');
		const row = page.getByTestId('rule-control_path');
		await expect(row.getByRole('heading')).toHaveText('Home Assistant access');
		await expect(radios(page, 'p-control_path-control_path')).toHaveText(['Hub only', 'Checked', 'Direct']);
		await expect(page.getByTestId('decided-quick_access')).toContainText('Needs the device\'s own Home Assistant key: not possible with “Hub only” without remote access');
		await expect(page.getByTestId('platform-key')).toContainText('On Apple TV, Android TV, or Apple Watch');
		await remote(page, 'On');
		await expect(page.getByTestId('remote-key-warning-profile')).toContainText('With “Hub only”, remote access gives each device with this profile a read-only Home Assistant key.');
		await page.getByTestId('save-preview').click();
		await expect(page.getByTestId('save-preview-remote-key-warning').first()).toContainText('remote access gives this device a read-only Home Assistant key');
		await page.keyboard.press('Escape');
		await page.goto('/profiles/p-child');
		await expect(page.getByTestId('child-protection-pro')).toHaveText(`Needs ${BRAND.name} Pro in the app on the child’s device.`);
		await page.goto('/profiles/p-tv');
		await expect(radios(page, 'p-control_path-control_path')).toHaveText(['Checked', 'Direct']);
		await page.goto('/clients/c-hubphone');
		await expect(page.getByTestId('ha-access')).toHaveText('Hub only');
		await expect(page.getByTestId('ha-key')).toHaveText('No key');
		await expect(page.getByTestId('ha-group')).toHaveText('Read-only (never admin)');
		await page.getByTestId('adjust-remote_access').click();
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'On' }).click();
		await expect(page.getByTestId('adjust-remote-key-warning')).toContainText('Switch this on only for people you trust.');
		await page.keyboard.press('Escape');
		await page.goto('/clients');
		await expect(page.getByTestId('ha-access-c-owner-a')).toContainText('Read-only key');
		await expect(page.getByTestId('ha-access-c-parent')).toContainText('Full key');
		await expect(page.getByTestId('ha-access-c-visitor')).toContainText('No key');
		expect(errors).toEqual([]);
	});
});

/**
 * Every flow that can leave a device on "Hub only" with remote access shows the
 * warning before anything is saved. The profile editor, the device adjustment
 * and the pairing dialog are covered above; these are the flows with a preview.
 */
test.describe('Every flow that hands out the read-only key shows the warning', () => {
	/** A member phone on the house sitter profile (Hub only, remote access on) with remote access adjusted off. */
	function withSitterPhone() {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-sitter', name: 'Sitter phone', profile_id: 'p-sitter' }));
		opts.overrides = { 'c-sitter': [{ rule: 'remote_access', value: false, user_choice: false, expires_at: null, note: null, created_by: 'admin', created_at: ago(5) }] };
		return opts;
	}

	test('assigning a profile: a device that keeps its key still sees the warning', async ({ page }) => {
		await boot(page);
		await page.goto('/clients/c-owner-a');
		await page.getByTestId('device-choose-profile').click();
		await page.getByTestId('profile-picker-p-sitter').check();
		await page.getByTestId('assign-preview-btn').click();
		const dev = page.getByTestId('assign-preview-device');
		await expect(dev).not.toContainText('Bekommt einen eigenen Home-Assistant-Schlüssel');
		await expect(page.getByTestId('assign-preview-remote-key-warning')).toBeVisible();
	});

	test('resetting one adjustment shows the preview with the warning first', async ({ page }) => {
		const { hub } = await boot(page, withSitterPhone());
		await page.goto('/clients/c-sitter');
		await page.getByTestId('reset-remote_access').click();
		await expect(page.getByTestId('reset-preview-dialog')).toBeVisible();
		await expect(page.getByTestId('reset-preview-remote-key-warning')).toBeVisible();
		expect(writes(hub, 'DELETE', '/pairing/clients/c-sitter/overrides/remote_access').map((r) => r[1])).toEqual(['/pairing/clients/c-sitter/overrides/remote_access?dry_run=true']);
		await expect(page.getByTestId('reset-preview-confirm')).toBeDisabled();
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('reset-preview-confirm').click();
		await expect(page.getByTestId('reset-preview-dialog')).toBeHidden();
		await expect(page.getByTestId('eff-remote_access')).toContainText('An');
		expect(writes(hub, 'DELETE', '/pairing/clients/c-sitter/overrides/remote_access')).toHaveLength(2);
	});

	test('resetting an adjustment that changes nothing risky resets at once', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/clients/c-owner-a');
		await page.getByTestId('adjust-control_mode_max').click();
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'Einfach' }).click();
		await page.getByTestId('adjust-save').click();
		await page.getByTestId('reset-control_mode_max').click();
		await expect(page.getByTestId('reset-preview-dialog')).toHaveCount(0);
		await expect.poll(() => writes(hub, 'DELETE', '/pairing/clients/c-owner-a/overrides/control_mode_max').length).toBe(2);
	});

	test('resetting everything to the profile', async ({ page }) => {
		await boot(page, withSitterPhone());
		await page.goto('/clients/c-sitter');
		await page.getByTestId('reset-all').click();
		await page.getByTestId('assign-preview-btn').click();
		await expect(page.getByTestId('assign-preview-remote-key-warning')).toBeVisible();
	});

	test('bulk: adjusting remote access and assigning a profile', async ({ page }) => {
		await boot(page);
		await page.goto('/clients');
		await page.getByTestId('select-c-hubphone').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-action').getByRole('radio', { name: 'Regel anpassen' }).click();
		await page.getByTestId('bulk-rule').selectOption('remote_access');
		await page.getByTestId('bulk-dialog').getByTestId('rule-value-remote_access').getByRole('radio', { name: 'An', exact: true }).click();
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-preview-detail-remote-key-warning')).toBeVisible();
		await page.keyboard.press('Escape');

		await page.goto('/clients');
		await page.getByTestId('select-c-owner-a').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-profile-p-sitter').check();
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-preview-detail-remote-key-warning')).toBeVisible();
	});

	test('bulk: removing an adjustment', async ({ page }) => {
		await boot(page, withSitterPhone());
		await page.goto('/clients');
		await page.getByTestId('select-c-sitter').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-action').getByRole('radio', { name: 'Anpassung entfernen' }).click();
		await page.getByTestId('bulk-rule').selectOption('remote_access');
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-preview-detail-remote-key-warning')).toBeVisible();
	});

	test('taking over a new suggestion version', async ({ page }) => {
		await boot(page, { ...household(), bumpSuggestion: { guest: 9 }, suggestionRules: { guest: { remote_access: true } } });
		await page.goto('/profiles/p-guest');
		await page.getByTestId('see-update').click();
		await expect(page.getByTestId('update-preview-remote-key-warning')).toBeVisible();
	});

	test('deleting a profile and moving its devices', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-hub');
		await page.getByTestId('profile-delete').click();
		await page.getByTestId('delete-replacement').selectOption('p-sitter');
		await page.getByTestId('delete-preview-btn').click();
		const warn = page.getByTestId('delete-preview-remote-key-warning');
		// The phone gets the key; the TV holds its read-only key anyway and is not on "Hub only".
		await expect(warn).toHaveCount(1);
		await expect(page.getByTestId('delete-preview-device').filter({ hasText: 'Hub phone' }).getByTestId('delete-preview-remote-key-warning')).toBeVisible();
	});

	test('profile editor: the warning also stands next to "Home Assistant access"', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-adult');
		await expect(page.getByTestId('remote-key-warning-path')).toHaveCount(0);
		await page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Nur über den Hub' }).click();
		await expect(page.getByTestId('remote-key-warning-path')).toBeVisible();
		await expect(page.getByTestId('remote-key-warning-profile')).toBeVisible();
	});

	test('pairing a guest on a person profile without a stored level warns (the guest is on "Hub only")', async ({ page }) => {
		const opts = household();
		const extra = opts.extra;
		opts.extra = (state, helpers) => {
			extra(state, helpers);
			const p = helpers.newProfile('p-visitor', 'adult', 'Besuch');
			delete p.rules.control_path;
			p.rules.remote_access = true;
			state.profiles.push(p);
		};
		await boot(page, opts);
		await page.goto('/guests');
		await page.getByTestId('open-pairing-guest').click();
		await page.getByTestId('pairing-profile-p-visitor').check();
		await expect(page.getByTestId('pairing-remote-key-warning')).toBeVisible();
		// As a member device the same profile is on "Checked": no new key, no warning.
		await page.getByTestId('pairing-role-member').click();
		await page.getByTestId('pairing-profile-p-visitor').check();
		await expect(page.getByTestId('pairing-remote-key-warning')).toHaveCount(0);
	});
});

test.describe('TVs and watches never end up on "Hub only"', () => {
	test('parental controls and the kind keep a TV profile on "Checked"', async ({ page }) => {
		await boot(page);
		await page.goto('/profiles/p-tv');
		await page.getByTestId('child-protection').click();
		await expect(page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Geprüft' })).toHaveAttribute('aria-checked', 'true');
		await page.goto('/profiles/p-hub');
		await page.getByTestId('profile-kind').selectOption('tv');
		await expect(radios(page, 'p-control_path-control_path')).toHaveText(['Geprüft', 'Direkt']);
		await expect(page.getByTestId('p-control_path-control_path').getByRole('radio', { name: 'Geprüft' })).toHaveAttribute('aria-checked', 'true');
	});

	test('a new TV profile with parental controls starts on "Checked"', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		await page.getByTestId('new-profile').click();
		await page.getByTestId('new-empty').click();
		await page.getByTestId('new-name').fill('TV Kinderzimmer');
		await page.getByTestId('new-kind').selectOption('tv');
		await page.getByTestId('new-child').click();
		await page.getByTestId('new-create').click();
		await expect(page.getByTestId('profile-title')).toHaveText('TV Kinderzimmer');
		expect(writes(hub, 'POST', '/profiles')[0][2].rules).toEqual({ control_path: 'checked' });
	});

	test('bulk does not offer "Hub only" while a TV is selected', async ({ page }) => {
		await boot(page);
		await page.goto('/clients');
		await page.getByTestId('select-c-tv').check();
		await page.getByTestId('select-c-owner-a').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-action').getByRole('radio', { name: 'Regel anpassen' }).click();
		await page.getByTestId('bulk-rule').selectOption('control_path');
		await expect(page.getByTestId('bulk-control_path').getByRole('radio')).toHaveText(['Geprüft', 'Direkt']);
		await expect(page.getByTestId('bulk-no-hub')).toBeVisible();
	});
});

test.describe('A device whose rules the hub cannot resolve', () => {
	function unresolved() {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-broken', name: 'Broken phone', profile_id: 'p-hub', unresolved: true }));
		return opts;
	}

	test('shows "unknown", never "no key"', async ({ page }) => {
		await boot(page, unresolved());
		await page.goto('/clients');
		const cell = page.getByTestId('ha-access-c-broken');
		await expect(cell).toHaveAttribute('data-level', 'unknown');
		await expect(cell).toContainText('Unbekannt');
		await expect(cell).toContainText('Schlüssel unbekannt');
		await page.goto('/clients/c-broken');
		await expect(page.getByTestId('ha-access')).toHaveText('Unbekannt');
		await expect(page.getByTestId('ha-key')).toHaveText('Schlüssel unbekannt');
		await expect(page.getByTestId('ha-key-hint')).toContainText('kann die Regeln dieses Geräts gerade nicht auflösen');
	});
});

test.describe('A scheduled end that leaves the device on "Hub only" with remote access', () => {
	/** Phones on the house sitter profile (Hub only, remote access on) with permanent adjustments. */
	function sitterPhones() {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-sitter', name: 'Sitter phone', profile_id: 'p-sitter' }));
		opts.clients.push(clientOf({ id: 'c-sitter2', name: 'Second sitter phone', profile_id: 'p-sitter' }));
		// The second phone has a permanent "Checked"; it can be adjusted again only in bulk (the device page offers the reset).
		const ov = (rule, value) => ({ rule, value, user_choice: false, expires_at: null, note: null, created_by: 'admin', created_at: ago(5) });
		opts.overrides = { 'c-sitter2': [ov('control_path', 'checked')] };
		opts.extra = ((extra) => (state, helpers) => {
			extra(state, helpers);
			// Nothing fixed: the adjustments above apply.
			state.profiles.find((p) => p.id === 'p-sitter').fixed = state.profiles.find((p) => p.id === 'p-sitter').fixed.filter((k) => k !== 'control_path');
		})(opts.extra);
		return opts;
	}

	test('a temporary "remote access off" over a profile with remote access on', async ({ page }) => {
		await boot(page, sitterPhones());
		await page.goto('/clients/c-sitter');
		await page.getByTestId('adjust-remote_access').click();
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'Aus', exact: true }).click();
		// Permanently off: nothing ends.
		await expect(page.getByTestId('adjust-remote-key-warning-end')).toHaveCount(0);
		await page.getByTestId('duration-1h').check();
		const warn = page.getByTestId('adjust-remote-key-warning-end');
		await expect(warn).toContainText('wenn ein befristeter Wert endet');
		await expect(warn).toContainText('bekommt das Gerät mit Fernzugriff einen Home-Assistant-Schlüssel');
		await expect(page.getByTestId('adjust-remote-key-warning')).toHaveCount(0);
	});

	test('a temporary "Direct" under a "Hub only" profile with remote access', async ({ page }) => {
		await boot(page, sitterPhones());
		await page.goto('/clients/c-sitter');
		await page.getByTestId('adjust-control_path').click();
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'Direkt' }).click();
		await expect(page.getByTestId('adjust-remote-key-warning-end')).toHaveCount(0);
		await page.getByTestId('duration-today').check();
		await expect(page.getByTestId('adjust-remote-key-warning-end')).toContainText('wenn ein befristeter Wert endet');
	});

	test('the bulk preview names the end', async ({ page }) => {
		await boot(page, sitterPhones());
		await page.goto('/clients');
		await page.getByTestId('select-c-sitter').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-action').getByRole('radio', { name: 'Regel anpassen' }).click();
		await page.getByTestId('bulk-rule').selectOption('remote_access');
		await page.getByTestId('bulk-dialog').getByTestId('rule-value-remote_access').getByRole('radio', { name: 'Aus', exact: true }).click();
		await page.getByTestId('bulk-dialog').getByRole('radio', { name: '1 Std.' }).check();
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-preview-detail-remote-key-warning-from')).toContainText('wenn ein befristeter Wert endet');
	});

	test('bulk: a temporary "Direct" over a permanent "Checked" names the end', async ({ page }) => {
		await boot(page, sitterPhones());
		await page.goto('/clients');
		await page.getByTestId('select-c-sitter2').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-action').getByRole('radio', { name: 'Regel anpassen' }).click();
		await page.getByTestId('bulk-rule').selectOption('control_path');
		await page.getByTestId('bulk-dialog').getByTestId('bulk-control_path').getByRole('radio', { name: 'Direkt' }).click();
		await page.getByTestId('bulk-dialog').getByRole('radio', { name: 'Heute' }).check();
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-preview-detail-remote-key-warning-from')).toContainText('wenn ein befristeter Wert endet');
	});
});

test.describe('Default profiles and guest extension', () => {
	test('a default that hands out the read-only key shows the warning', async ({ page }) => {
		const { hub } = await boot(page);
		await page.goto('/profiles');
		const card = page.getByTestId('default-profiles');
		await expect(card.getByTestId('default-remote-key-warning')).toHaveCount(0);
		// A TV never runs on "Hub only": no warning.
		await page.getByTestId('default-tvos').selectOption('p-sitter');
		await expect(card.getByTestId('default-remote-key-warning')).toHaveCount(0);
		await page.getByTestId('default-ipados').selectOption('p-sitter');
		await page.getByTestId('default-guest').selectOption('p-sitter');
		await expect(card.getByTestId('default-remote-key-slots')).toContainText('iPad, Gäste');
		await expect(card.getByTestId('default-remote-key-warning')).toBeVisible();
		await page.getByTestId('default-profiles-save').click();
		expect(writes(hub, 'PUT', '/settings/profiles')[0][2].default_profiles.guest).toBe('p-sitter');
	});

	test('extending an expired guest on "Hub only" with remote access warns before "+1 day"', async ({ page }) => {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-gone', name: 'Former guest phone', access: 'guest', expires_at: ago(60), profile_id: 'p-sitter' }));
		await boot(page, opts);
		await page.goto('/guests');
		const card = page.getByTestId('guest-card').filter({ hasText: 'Former guest phone' });
		await card.getByRole('button').filter({ hasText: '+1 Tag' }).click();
		await expect(page.getByTestId('guest-extend-dialog').getByTestId('guest-extend-remote-key-warning')).toBeVisible();
	});
});

test.describe('Unknown remote access and TVs on a "Hub only" profile', () => {
	test('an unresolved device shows remote access as unknown and suggests the stronger removal', async ({ page }) => {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-broken', name: 'Broken phone', profile_id: 'p-hub', unresolved: true, is_connected: false }));
		opts.clients.push(clientOf({ id: 'c-broken-guest', name: 'Broken guest phone', access: 'guest', expires_at: new Date(Date.now() + 86400_000).toISOString(), profile_id: 'p-guest', unresolved: true }));
		await boot(page, opts);
		await page.goto('/clients');
		await expect(page.getByTestId('remote-c-broken')).toHaveAttribute('data-state', 'unknown');
		await expect(page.getByTestId('remote-c-broken')).toContainText('Fernzugriff unbekannt');
		await page.goto('/guests');
		await expect(page.getByTestId('guest-card').filter({ hasText: 'Broken guest phone' }).getByTestId('guest-remote')).toHaveAttribute('data-state', 'unknown');
		await page.goto('/clients/c-broken');
		await expect(page.getByTestId('fact-remote')).toHaveAttribute('data-state', 'unknown');
		await page.getByTestId('device-remove').click();
		await expect(page.getByTestId('removal-immediate')).toBeChecked();
		await expect(page.getByTestId('recommendation-reason')).toContainText('unbekannt, ob es Fernzugriff hat');
	});

	test('a TV on a "Hub only" profile is shown on "Checked" with the reason', async ({ page }) => {
		await boot(page);
		await page.goto('/clients');
		await expect(page.getByTestId('ha-access-c-tv2')).toHaveAttribute('data-level', 'checked');
		await expect(page.getByTestId('ha-access-reason-c-tv2')).toContainText('nie ohne Schlüssel');
		await expect(page.getByTestId('ha-access-reason-c-hubphone')).toHaveCount(0);
		await page.goto('/clients/c-tv2');
		await expect(page.getByTestId('ha-access-reason')).toContainText('Auf Android TV');
		// Moving a TV to a "Hub only" profile: the preview says why it stays on "Checked".
		await page.goto('/clients/c-tv');
		await page.getByTestId('device-choose-profile').click();
		await page.getByTestId('profile-picker-p-hub').check();
		await page.getByTestId('assign-preview-btn').click();
		await expect(page.getByTestId('assign-preview-platform-key')).toContainText('bleibt darum auf „Geprüft“');
	});
});

test.describe('The hub decides: hub_remote_now and hub_remote_at', () => {
	const later = () => new Date(Date.now() + 2 * 3600_000).toISOString();
	const ov = (rule, value, expires_at = null) => ({ rule, value, user_choice: false, expires_at, note: null, created_by: 'admin', created_at: ago(5) });
	/** The hub phone has a temporary "Checked"; the first adult phone has remote access off for a while. */
	function scheduled() {
		const opts = household();
		opts.overrides = { 'c-hubphone': [ov('control_path', 'checked', later())], 'c-owner-a': [ov('remote_access', false, later())] };
		return opts;
	}

	test('temporary "Checked" until later, then remote access on permanently', async ({ page }) => {
		await boot(page, scheduled());
		await page.goto('/clients/c-hubphone');
		await page.getByTestId('adjust-remote_access').click();
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'An', exact: true }).click();
		// Now the phone is on "Checked"; when its temporary "Checked" ends, it is on "Hub only" with remote access.
		await expect(page.getByTestId('adjust-remote-key-warning-end')).toContainText('wenn ein befristeter Wert endet');
		await expect(page.getByTestId('adjust-remote-key-warning')).toHaveCount(0);
		// Saving waits for the hub's answer and the loosening confirmation.
		await page.getByTestId('loosening-confirm').check();
		await expect(page.getByTestId('adjust-save')).toBeEnabled();
	});

	test('remote access off until later, then the level set permanently to "Hub only"', async ({ page }) => {
		await boot(page, scheduled());
		await page.goto('/clients/c-owner-a');
		await page.getByTestId('adjust-control_path').click();
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'Nur über den Hub' }).click();
		await expect(page.getByTestId('adjust-remote-key-warning-end')).toContainText('wenn ein befristeter Wert endet');
		await expect(page.getByTestId('adjust-remote-key-warning')).toHaveCount(0);
	});

	test('the same two through bulk edit', async ({ page }) => {
		await boot(page, scheduled());
		await page.goto('/clients');
		await page.getByTestId('select-c-owner-a').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-action').getByRole('radio', { name: 'Regel anpassen' }).click();
		await page.getByTestId('bulk-rule').selectOption('control_path');
		await page.getByTestId('bulk-dialog').getByTestId('bulk-control_path').getByRole('radio', { name: 'Nur über den Hub' }).click();
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-preview-detail-remote-key-warning-from')).toContainText('wenn ein befristeter Wert endet');

		await page.goto('/clients');
		await page.getByTestId('select-c-hubphone').check();
		await page.getByTestId('bulk-open').click();
		await page.getByTestId('bulk-action').getByRole('radio', { name: 'Regel anpassen' }).click();
		await page.getByTestId('bulk-rule').selectOption('remote_access');
		await page.getByTestId('bulk-dialog').getByTestId('rule-value-remote_access').getByRole('radio', { name: 'An', exact: true }).click();
		await page.getByTestId('bulk-preview').click();
		await expect(page.getByTestId('bulk-preview-detail-remote-key-warning-from')).toContainText('wenn ein befristeter Wert endet');
	});

	test('assigning a "Hub only" profile with remote access while keeping the adjustments', async ({ page }) => {
		const { hub } = await boot(page, scheduled());
		await page.goto('/clients/c-owner-a');
		await page.getByTestId('device-choose-profile').click();
		await page.getByTestId('profile-picker-p-sitter').check();
		await page.getByTestId('assign-preview-btn').click();
		await expect(page.getByTestId('assign-preview-remote-key-warning-from')).toContainText('wenn ein befristeter Wert endet');
		expect(writes(hub, 'PUT', '/pairing/clients/c-owner-a/profile')[0][2]).toEqual({ profile_id: 'p-sitter', discard_overrides: false });
	});

	test('a hub without the two fields: the warning shows when unsure', async ({ page }) => {
		const { hub } = await boot(page, scheduled());
		const handler = hub.handler;
		await page.unrouteAll();
		await setupApiMocks(page, {
			areas: AREAS,
			handler: async (args) => {
				const res = await handler({ ...args, json: (body, status) => ({ body, status }) });
				if (res && res.body?.devices) for (const d of res.body.devices) delete d.hub_remote_now, delete d.hub_remote_at;
				return res ? args.json(res.body, res.status) : undefined;
			},
		});
		await page.goto('/clients/c-hubphone');
		await page.getByTestId('adjust-remote_access').click();
		await page.getByTestId('adjust-dialog').getByRole('radio', { name: 'An', exact: true }).click();
		await expect(page.getByTestId('adjust-remote-key-warning')).toBeVisible();
	});
});

test.describe('Shaped like the hub: listed-only devices and the pairing answer', () => {
	test('an unchanged device is listed only when its pair changes', async ({ page }) => {
		// Like the hub: a device on "Hub only" with remote access that the change leaves as it is is not listed.
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-sitter', name: 'Sitter phone', profile_id: 'p-sitter' }));
		await boot(page, opts);
		await page.goto('/profiles/p-adult');
		await page.getByTestId('p-card_style-card_style').getByRole('radio', { checked: false }).first().click();
		await page.getByTestId('save-preview').click();
		await expect(page.getByTestId('save-preview-count')).toContainText('2 Geräte');
		await expect(page.getByTestId('save-preview-device')).toHaveCount(2);
		await expect(page.getByTestId('save-preview-listed')).toHaveCount(0);
	});

	test('listed-only entries show apart, and only those that warn', async ({ page }) => {
		const { hub } = await boot(page);
		const handler = hub.handler;
		await page.unrouteAll();
		await setupApiMocks(page, {
			areas: AREAS,
			handler: async (args) => {
				const res = await handler({ ...args, json: (body, status) => ({ body, status }) });
				if (!res) return undefined;
				if (res.body?.dry_run && Array.isArray(res.body.devices)) {
					const entry = (client_id, name, now, at) => ({ client_id, name, changes: [], overrides_removed: [], ha_key: 'unchanged', remote_access: 'unchanged', hub_remote_now: now, hub_remote_at: at });
					res.body.devices.push(entry('c-hubphone', 'Hub phone', true, null), entry('c-tv2', 'Kitchen TV', false, null));
				}
				return args.json(res.body, res.status);
			},
		});
		await page.goto('/profiles/p-adult');
		await page.getByTestId('p-card_style-card_style').getByRole('radio', { checked: false }).first().click();
		await page.getByTestId('save-preview').click();
		await expect(page.getByTestId('save-preview-count')).toContainText('2 Geräte');
		await expect(page.getByTestId('save-preview-device')).toHaveCount(2);
		await expect(page.getByTestId('save-preview-listed-device')).toHaveCount(1);
		await expect(page.getByTestId('save-preview-listed-device')).toContainText('Hub phone');
		await expect(page.getByTestId('save-preview-listed-device').getByTestId('save-preview-remote-key-warning')).toBeVisible();
		await expect(page.getByTestId('save-preview-listed')).toContainText('nicht betroffen');
	});

	/** Rewrites every profile in GET /profiles and /profiles/{id}: `edit(profile)`. */
	async function bootWithProfiles(page, edit) {
		const { hub } = await boot(page);
		const handler = hub.handler;
		await page.unrouteAll();
		await setupApiMocks(page, {
			areas: AREAS,
			handler: async (args) => {
				const res = await handler({ ...args, json: (body, status) => ({ body, status }) });
				if (!res) return undefined;
				if (args.method === 'GET' && /^\/profiles(\/[^/]+)?$/.test(args.path)) {
					for (const p of res.body?.profiles || [res.body]) if (p?.id) edit(p);
				}
				return args.json(res.body, res.status);
			},
		});
		return hub;
	}

	test('the pairing dialog and the default profiles follow the hub\'s answer', async ({ page }) => {
		// The hub says no device paired with the house sitter profile would be on "Hub only" with remote access.
		await bootWithProfiles(page, (p) => {
			if (p.id !== 'p-sitter') return;
			p.rules.remote_access = true;
			for (const a of ['member', 'guest']) for (const k of Object.keys(p.hub_remote_pairing[a])) p.hub_remote_pairing[a][k] = false;
		});
		await page.goto('/guests');
		await page.getByTestId('open-pairing-guest').click();
		await page.getByTestId('pairing-profile-p-sitter').check();
		await expect(page.getByTestId('pairing-remote-key-warning')).toHaveCount(0);
		await page.keyboard.press('Escape');
		await page.goto('/profiles');
		await page.getByTestId('default-guest').selectOption('p-sitter');
		await expect(page.getByTestId('default-remote-key-warning')).toHaveCount(0);
	});

	test('without the hub\'s answer the profile\'s own values decide', async ({ page }) => {
		await bootWithProfiles(page, (p) => delete p.hub_remote_pairing);
		await page.goto('/guests');
		await page.getByTestId('open-pairing-guest').click();
		await page.getByTestId('pairing-profile-p-sitter').check();
		await expect(page.getByTestId('pairing-remote-key-warning')).toBeVisible();
	});
});

test.describe('Extending a guest asks the hub first', () => {
	const inH = (h) => new Date(Date.now() + h * 3600_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
	/** A guest on the house sitter profile (Hub only, remote access on) with remote access off until `offH` hours, access ending in 2 hours. */
	function guestWithEnd(offH) {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-sitguest', name: 'Sitter guest phone', access: 'guest', expires_at: inH(2), profile_id: 'p-sitter' }));
		const end = inH(offH);
		opts.overrides = { 'c-sitguest': [{ rule: 'remote_access', value: false, user_choice: false, expires_at: end, note: null, created_by: 'admin', created_at: ago(5) }] };
		return { opts, end };
	}

	test('device page: extending to 48 hours warns with the date remote access comes back', async ({ page }) => {
		const { opts, end } = guestWithEnd(30);
		const { hub } = await boot(page, { ...opts, extendDelay: 400 });
		await page.goto('/clients/c-sitguest');
		await page.getByTestId('device-extend').click();
		await page.getByTestId('extend-quick-48').click();
		// Extend waits for the hub's answer to the newest choice.
		await expect(page.getByTestId('extend-save')).toBeDisabled();
		const from = page.getByTestId('extend-remote-key-warning-from');
		await expect(from).toHaveAttribute('data-at', end);
		await expect(from).toContainText('wenn ein befristeter Wert endet');
		await expect(page.getByTestId('extend-save')).toBeEnabled();
		// A shorter extension ends before remote access comes back: no warning.
		await page.getByTestId('extend-quick-24').click();
		await expect(page.getByTestId('extend-remote-key-warning')).toHaveCount(0);
		await page.getByTestId('extend-quick-48').click();
		await expect(from).toBeVisible();
		await page.getByTestId('extend-save').click();
		const writes = hub.requests.filter(([m, p]) => m === 'PATCH' && p === '/clients/c-sitguest/extend').map((r) => r[2]);
		expect(writes.at(-1)).toEqual({ hours: 48 });
		expect(writes.filter((b) => b.dry_run).length).toBeGreaterThan(0);
	});

	test('guests page: "+1 day" asks first when the extension leads to the read-only key', async ({ page }) => {
		// "+1 day" sets the end to 24 hours from now: remote access comes back at 20 hours.
		const { opts, end } = guestWithEnd(20);
		const { hub } = await boot(page, opts);
		await page.goto('/guests');
		const card = page.getByTestId('guest-card').filter({ hasText: 'Sitter guest phone' });
		await card.getByRole('button').filter({ hasText: '+1 Tag' }).click();
		const dlg = page.getByTestId('guest-extend-dialog');
		await expect(dlg.getByTestId('guest-extend-remote-key-warning-from')).toHaveAttribute('data-at', end);
		const writes = () => hub.requests.filter(([m, p]) => m === 'PATCH' && p === '/clients/c-sitguest/extend').map((r) => r[2]);
		expect(writes()).toEqual([{ hours: 24, dry_run: true }]);
		await page.getByTestId('guest-extend-confirm').click();
		await expect.poll(() => writes().length).toBe(2);
		expect(writes()[1]).toEqual({ hours: 24 });
		// A guest whose extension hands out no key is extended at once.
		const plain = page.getByTestId('guest-card').filter({ hasText: 'iPhone Visitor' });
		await plain.getByRole('button').filter({ hasText: '+1 Tag' }).click();
		await expect.poll(() => hub.requests.filter(([m, p]) => m === 'PATCH' && p === '/clients/c-visitor/extend').length).toBe(2);
		await expect(page.getByTestId('guest-extend-dialog')).toBeHidden();
	});

	test('no answer from the hub: the warning shows, the extension is not blocked', async ({ page }) => {
		const { opts } = guestWithEnd(20);
		await boot(page, opts);
		await page.route('**/api/clients/*/extend', async (route) => {
			const body = route.request().postDataJSON();
			if (body?.dry_run) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"error"}' });
			return route.fallback();
		});
		await page.goto('/guests');
		await page.getByTestId('guest-card').filter({ hasText: 'iPhone Visitor' }).getByRole('button').filter({ hasText: '+1 Tag' }).click();
		const dlg = page.getByTestId('guest-extend-dialog');
		await expect(dlg.getByTestId('guest-extend-unknown')).toBeVisible();
		await expect(dlg.getByTestId('guest-extend-remote-key-warning')).toBeVisible();
		await expect(page.getByTestId('guest-extend-confirm')).toBeEnabled();
	});
});

test.describe('A preview always belongs to the current inputs', () => {
	test('assign: a late preview of another profile is dropped', async ({ page }) => {
		const { hub } = await boot(page, { ...household(), previewDelay: (path, body) => (body?.profile_id === 'p-sitter' ? 1500 : 0) });
		await page.goto('/clients/c-owner-a');
		await page.getByTestId('device-choose-profile').click();
		await page.getByTestId('profile-picker-p-sitter').check();
		await page.getByTestId('assign-preview-btn').click();
		// While the slow answer is out, another profile is picked and previewed.
		await page.getByTestId('profile-picker-p-wall').check();
		await page.getByTestId('assign-preview-btn').click();
		await expect(page.getByTestId('assign-save')).toBeEnabled();
		await page.waitForTimeout(1700);
		// The late answer for the house sitter profile did not replace the current preview.
		await expect(page.getByTestId('assign-preview-remote-key-warning')).toHaveCount(0);
		await expect(page.getByTestId('assign-save')).toBeEnabled();
		await page.getByTestId('assign-save').click();
		const writes = hub.requests.filter(([m, p]) => m === 'PUT' && p === '/pairing/clients/c-owner-a/profile').map((r) => r[2]);
		expect(writes.at(-1)).toEqual({ profile_id: 'p-wall', discard_overrides: false });
	});

	test('assign: picking another profile while the preview is out keeps saving disabled', async ({ page }) => {
		await boot(page, { ...household(), previewDelay: 1200 });
		await page.goto('/clients/c-owner-a');
		await page.getByTestId('device-choose-profile').click();
		await page.getByTestId('profile-picker-p-sitter').check();
		await page.getByTestId('assign-preview-btn').click();
		await page.getByTestId('profile-picker-p-wall').check();
		await page.waitForTimeout(1500);
		await expect(page.getByTestId('assign-save')).toHaveCount(0);
		await expect(page.getByTestId('assign-preview-btn')).toBeEnabled();
	});

	test('delete: a late preview of another replacement is dropped', async ({ page }) => {
		const { hub } = await boot(page, { ...household(), previewDelay: (path) => (path.includes('replacement=p-sitter') ? 1500 : 0) });
		await page.goto('/profiles/p-hub');
		await page.getByTestId('profile-delete').click();
		await page.getByTestId('delete-replacement').selectOption('p-sitter');
		await page.getByTestId('delete-preview-btn').click();
		await page.getByTestId('delete-replacement').selectOption('p-wall');
		await expect(page.getByTestId('delete-confirm')).toHaveCount(0);
		await page.getByTestId('delete-preview-btn').click();
		await expect(page.getByTestId('delete-confirm')).toBeVisible();
		await page.waitForTimeout(1700);
		await expect(page.getByTestId('delete-preview-remote-key-warning')).toHaveCount(0);
		await page.getByTestId('loosening-confirm').check();
		await page.getByTestId('delete-confirm').click();
		await expect(page).toHaveURL(/\/profiles$/);
		expect(hub.requests.filter(([m, p]) => m === 'DELETE' && !p.includes('dry_run')).map((r) => r[1])).toEqual(['/profiles/p-hub?replacement=p-wall']);
	});
});

test.describe('Extending: one guest at a time, and never waiting for good', () => {
	const inH = (h) => new Date(Date.now() + h * 3600_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
	const ov = (end) => [{ rule: 'remote_access', value: false, user_choice: false, expires_at: end, note: null, created_by: 'admin', created_at: ago(5) }];
	function twoGuests() {
		const opts = household();
		opts.clients.push(clientOf({ id: 'c-sitguest', name: 'Sitter guest phone', access: 'guest', expires_at: inH(2), profile_id: 'p-sitter' }));
		opts.clients.push(clientOf({ id: 'c-sitguest2', name: 'Second sitter guest', access: 'guest', expires_at: inH(2), profile_id: 'p-sitter' }));
		opts.overrides = { 'c-sitguest': ov(inH(20)), 'c-sitguest2': ov(inH(20)) };
		return opts;
	}
	const realWrites = (hub) => hub.requests.filter(([m, p, b]) => m === 'PATCH' && p.endsWith('/extend') && !b?.dry_run).map((r) => r[1]);

	test('guests page: a second "+1 day" waits until the first is answered and closed', async ({ page }) => {
		const { hub } = await boot(page, { ...twoGuests(), previewDelay: (p) => (p.includes('c-sitguest/') ? 1500 : 0) });
		await page.goto('/guests');
		const a = page.getByTestId('guest-card').filter({ hasText: 'Sitter guest phone' }).getByRole('button').filter({ hasText: '+1 Tag' });
		const b = page.getByTestId('guest-card').filter({ hasText: 'Second sitter guest' }).getByRole('button').filter({ hasText: '+1 Tag' });
		await a.click();
		// While the answer for the first guest is out, no other "+1 day" can start.
		await expect(b).toBeDisabled();
		const dlg = page.getByTestId('guest-extend-dialog');
		await expect(dlg).toContainText('Sitter guest phone');
		// While the dialog is open, too.
		await expect(b).toBeDisabled();
		await page.getByTestId('guest-extend-confirm').click();
		await expect.poll(() => realWrites(hub)).toEqual(['/clients/c-sitguest/extend']);
		await expect(b).toBeEnabled();
		await b.click();
		await expect(dlg).toContainText('Second sitter guest');
	});

	test('a dry run without an answer: after the deadline the warning shows and Extend works', async ({ page }) => {
		test.setTimeout(45_000);
		await boot(page, twoGuests());
		await page.route('**/api/clients/*/extend', async (route) => {
			if (route.request().postDataJSON()?.dry_run) return; // never answered
			return route.fallback();
		});
		await page.goto('/clients/c-sitguest');
		await page.getByTestId('device-extend').click();
		await expect(page.getByTestId('extend-save')).toBeDisabled();
		await expect(page.getByTestId('extend-unknown')).toBeVisible({ timeout: 15_000 });
		await expect(page.getByTestId('extend-remote-key-warning')).toBeVisible();
		await expect(page.getByTestId('extend-save')).toBeEnabled();
	});

	test('an answer that is not a dry run counts as no answer', async ({ page }) => {
		await boot(page, twoGuests());
		await page.route('**/api/clients/*/extend', async (route) => {
			if (route.request().postDataJSON()?.dry_run) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ client_id: 'c-sitguest', expires_at: inH(24), hub_remote_now: false, hub_remote_at: null }) });
			return route.fallback();
		});
		await page.goto('/guests');
		await page.getByTestId('guest-card').filter({ hasText: 'Sitter guest phone' }).getByRole('button').filter({ hasText: '+1 Tag' }).click();
		const dlg = page.getByTestId('guest-extend-dialog');
		await expect(dlg.getByTestId('guest-extend-unknown')).toContainText('vorsichtshalber');
		await expect(dlg.getByTestId('guest-extend-remote-key-warning')).toBeVisible();
	});
});
