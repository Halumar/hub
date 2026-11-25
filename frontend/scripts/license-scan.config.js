// Vite config for the third-party notice scan (scripts/third_party_notices.py).
//
//   npx vite build --config scripts/license-scan.config.js
//
// Builds the Admin UI exactly like `npm run build` (same output in
// halumar/rootfs/opt/hub/static) and records which npm packages end up in the
// browser bundle: every module from node_modules in the client chunks, plus
// packages pulled in with CSS @import (Tailwind). SvelteKit runs the client
// build as a nested build with this config file, so the plugin sees it.
// Result: node_modules/.cache/third-party/bundle-packages.json
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mergeConfig } from 'vite';
import base from '../vite.config.js';

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const OUT = join(FRONTEND, 'node_modules/.cache/third-party/bundle-packages.json');
const PKG_RE = /node_modules\/((?:@[^/]+\/)?[^/]+)\//;

function cssImports(dir, found = new Set()) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) cssImports(path, found);
		else if (/\.(css|svelte)$/.test(entry.name)) {
			for (const m of readFileSync(path, 'utf8').matchAll(/@import\s+["']([^"'./][^"']*)["']/g)) {
				found.add(m[1].startsWith('@') ? m[1].split('/').slice(0, 2).join('/') : m[1].split('/')[0]);
			}
		}
	}
	return found;
}

function collectBundlePackages() {
	return {
		name: 'third-party-bundle-packages',
		apply: 'build',
		generateBundle(_options, bundle) {
			if (this.environment?.name !== 'client') return;
			const packages = cssImports(join(FRONTEND, 'src'));
			for (const chunk of Object.values(bundle)) {
				if (chunk.type !== 'chunk') continue;
				for (const id of chunk.moduleIds) {
					const m = id.replaceAll('\\', '/').match(PKG_RE);
					if (m) packages.add(m[1]);
				}
			}
			mkdirSync(dirname(OUT), { recursive: true });
			writeFileSync(OUT, JSON.stringify([...packages].sort(), null, 2) + '\n');
		},
	};
}

export default mergeConfig(base, { plugins: [collectBundlePackages()] });
