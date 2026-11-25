// Web app manifest of the admin UI (direct access in the home network; under Home Assistant
// Ingress the page runs in a frame and browsers ignore it). Name from brand.json, icons from
// the brand kit (web/): maskable icons on terracotta, the symbol inside the safe zone.
import { BRAND } from '$lib/brand.ts';

export const prerender = true;

export function GET() {
	const manifest = {
		name: BRAND.name,
		short_name: BRAND.shortName,
		description: BRAND.description,
		start_url: './',
		scope: './',
		display: 'standalone',
		// Sand (kit tokens) behind the splash; the bar follows the page.
		background_color: '#F4EADB',
		theme_color: '#F4EADB',
		icons: [
			{ src: 'favicon.svg', type: 'image/svg+xml', sizes: 'any', purpose: 'any' },
			{ src: 'apple-touch-icon.png', type: 'image/png', sizes: '180x180', purpose: 'any' },
			{ src: 'icon-maskable-192.png', type: 'image/png', sizes: '192x192', purpose: 'maskable' },
			{ src: 'icon-maskable-512.png', type: 'image/png', sizes: '512x512', purpose: 'maskable' },
		],
	};
	return new Response(JSON.stringify(manifest, null, '\t'), {
		headers: { 'content-type': 'application/manifest+json' },
	});
}
