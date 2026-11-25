<script>
	/**
	 * Symbol of a profile or suggestion ("mdi:human-child") on its color.
	 * The MDI path catalog is loaded lazily (own chunk); until then and for
	 * unknown names a neutral profile symbol is shown. Decorative.
	 */
	import { loadMdiIcons } from '$lib/data/mdi-lazy.js';
	import Icon from '$lib/ui/Icon.svelte';

	let { icon = '', color = '', size = 36 } = $props();

	let path = $state('');

	$effect(() => {
		const name = (icon || '').replace(/^mdi:/, '');
		path = '';
		if (!name) return;
		let alive = true;
		loadMdiIcons()
			.then(({ map }) => {
				if (alive) path = map.get(name) || '';
			})
			.catch(() => {});
		return () => {
			alive = false;
		};
	});
</script>

<span class="pi" class:tinted={!!color} style:--pc={color || null} style:--ps="{size}px" aria-hidden="true">
	{#if path}
		<svg viewBox="0 0 24 24"><path d={path} /></svg>
	{:else}
		<Icon name="template" />
	{/if}
</span>

<style>
	.pi {
		width: var(--ps);
		height: var(--ps);
		flex: none;
		border-radius: 10px;
		display: grid;
		place-items: center;
		background: var(--fill-2);
		color: var(--ink-2);
	}
	.pi.tinted {
		background: color-mix(in srgb, var(--pc) 16%, var(--surface));
		color: color-mix(in srgb, var(--pc) 75%, var(--ink));
	}
	.pi svg,
	.pi :global(.ic) {
		width: 58%;
		height: 58%;
	}
	/* MDI paths are filled; the fallback symbol (Icon) is drawn with strokes. */
	.pi svg {
		fill: currentColor;
	}
</style>
