import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	kit: {
		adapter: adapter({
			pages: '../halumar/rootfs/opt/hub/static',
			assets: '../halumar/rootfs/opt/hub/static',
			fallback: 'index.html',
			precompress: false,
			strict: false
		}),
		paths: {
			relative: true
		},
		prerender: {
			handleHttpError: 'warn',
			handleUnseenRoutes: 'warn'
		}
	}
};

export default config;
