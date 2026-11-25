import { readFileSync } from 'node:fs';
import { test, expect } from '@playwright/test';
import en from '../src/lib/i18n/en/index.js';
import de from '../src/lib/i18n/de/index.js';
import { CATALOG } from './profiles-hub.js';

/**
 * Every value the hub can send and the admin UI shows as text has a text in
 * English and German: contract enums (e2e/fixtures/contract/enums.json),
 * the catalog's reasons and boundaries, and the key states of the device list.
 */
const ENUMS = JSON.parse(readFileSync(new URL('./fixtures/contract/enums.json', import.meta.url), 'utf8'));
const DICTS = { en, de };

const at = (dict, path) => path.split('.').reduce((o, k) => (o && typeof o === 'object' ? o[k] : undefined), dict);
const missing = (path) => Object.entries(DICTS).filter(([, d]) => !String(at(d, path) || '').trim()).map(([l]) => `${l}: ${path}`);

/** Rule reasons the admin UI words itself; every other one takes the catalog's sentence. */
const UI_REASONS = { profile: 'profiles.src.profile', user_choice: 'profiles.src.userChoice', device: 'profiles.src.device', device_temporary: 'profiles.src.temporary' };
/** Key states of the device list and page (see haAccessOf in src/lib/device-info.js). */
const KEY_STATES = ['none', 'read_only', 'full', 'revoking', 'pending', 'unknown'];

test('every watch removal reason has a text', () => {
	expect(ENUMS.watch_removed_reason.flatMap((r) => missing(`profiles.preview.watchReason.${r}`))).toEqual([]);
});

test('every rule reason has a text, from the UI or the catalog', () => {
	const gaps = [];
	for (const code of ENUMS.rule_reason) {
		if (UI_REASONS[code]) {
			gaps.push(...missing(UI_REASONS[code]));
			continue;
		}
		const r = CATALOG.reasons.find((x) => x.code === code);
		for (const l of ['en', 'de']) if (!r?.admin?.[l]?.trim()) gaps.push(`${l}: catalog reason ${code}`);
	}
	expect(gaps).toEqual([]);
});

test('every boundary has a label and its effects in the catalog', () => {
	const gaps = [];
	for (const id of ENUMS.rule_boundary) {
		const b = CATALOG.boundaries.find((x) => x.id === id);
		for (const l of ['en', 'de']) if (!b?.label?.[l]?.trim() || !b?.effects?.[l]?.trim()) gaps.push(`${l}: boundary ${id}`);
	}
	expect(gaps).toEqual([]);
});

test('every access level and key state has a text', () => {
	const gaps = [...ENUMS.control_path_rule.flatMap((v) => missing(`profiles.enum.control_path.${v}`)), ...missing('devices.levelUnknown')];
	for (const k of KEY_STATES) {
		gaps.push(...missing(`devices.key.${k}`));
		if (k !== 'pending') gaps.push(...missing(`devices.keyHint.${k}`));
	}
	expect(gaps).toEqual([]);
});
