#!/usr/bin/env node
// Fails on a known vulnerability in a package that ships in the Admin UI bundle.
//
//   npm audit --json > audit.json || true
//   node scripts/ci/npm-audit-bundle.mjs audit.json bundle-packages.json allow.txt
//
// `npm audit --omit=dev` alone misses them: svelte, @sveltejs/kit and qrcode are
// devDependencies (the bundler inlines them) but run in the browser. The package
// list comes from `npm run licenses` (frontend/scripts/license-scan.config.js).
// allow.txt: one advisory id (GHSA-…) per line with the reason after `#`; only for
// advisories that cannot affect the static bundle (server-side code paths).
import { readFileSync } from 'node:fs';

const [auditPath, bundlePath, allowPath] = process.argv.slice(2);
if (!auditPath || !bundlePath || !allowPath) {
	console.error('usage: npm-audit-bundle.mjs <audit.json> <bundle-packages.json> <allow.txt>');
	process.exit(2);
}

const audit = JSON.parse(readFileSync(auditPath, 'utf8'));
if (!audit.vulnerabilities) {
	console.error('npm audit produced no report:', JSON.stringify(audit.error ?? audit).slice(0, 500));
	process.exit(2);
}
const bundled = new Set(JSON.parse(readFileSync(bundlePath, 'utf8')));
const allowed = new Map();
for (const line of readFileSync(allowPath, 'utf8').split('\n')) {
	const [id, ...reason] = line.split('#');
	if (id.trim()) allowed.set(id.trim(), reason.join('#').trim());
}

const failures = [];
const used = new Set();
for (const [name, vuln] of Object.entries(audit.vulnerabilities)) {
	if (!bundled.has(name)) continue;
	for (const via of vuln.via) {
		// A string names another package; its advisory is listed under that name.
		if (typeof via === 'string') continue;
		const id = (via.url ?? '').split('/').pop() || String(via.source);
		if (allowed.has(id)) {
			used.add(id);
			console.log(`allowed  ${name} ${id} (${via.severity}): ${allowed.get(id)}`);
		} else {
			failures.push(`${name} ${id} (${via.severity}, ${via.range}): ${via.title}`);
		}
	}
}
for (const id of allowed.keys()) {
	if (!used.has(id)) console.log(`::warning::${id} is in the allow list but no longer reported; remove it`);
}
if (failures.length) {
	for (const f of failures) console.log(`::error::bundled package with a known vulnerability: ${f}`);
	process.exit(1);
}
console.log(`No unallowed advisory for the ${bundled.size} bundled packages.`);
