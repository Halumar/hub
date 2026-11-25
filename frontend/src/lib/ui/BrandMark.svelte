<script>
	/**
	 * The brand symbol (house h with window) as inline SVG from the kit outlines.
	 * size: rendered height in px; under 32 px the small cut is used.
	 * state: 'idle' | 'loading' (window breathes 0.82 <-> 1, 1.6 s) | 'offline' (window as outline).
	 * label: accessible name; without it the mark is decorative.
	 */
	import { SYMBOL, SYMBOL_SMALL } from './brand-mark.js';
	let { size = 28, state = 'idle', label = '', class: cls = '' } = $props();
	let cut = $derived(size < SYMBOL_SMALL.below ? SYMBOL_SMALL : SYMBOL);
	let ratio = $derived.by(() => {
		const [, , w, h] = cut.viewBox.split(' ').map(Number);
		return w / h;
	});
	// The outline lies inside the window square (inset by half the stroke), so the window keeps its size.
	const OUTLINE = 2;
</script>

<svg
	class="mark {cls}"
	class:loading={state === 'loading'}
	class:offline={state === 'offline'}
	viewBox={cut.viewBox}
	height={size}
	width={size * ratio}
	role={label ? 'img' : undefined}
	aria-label={label || undefined}
	aria-hidden={label ? undefined : 'true'}
	focusable="false"
	data-testid="brand-mark"
	data-state={state}
>
	<path class="house" d={cut.path} />
	{#if state === 'offline'}
		<rect
			class="win-outline"
			x={cut.window.x + OUTLINE / 2}
			y={cut.window.y + OUTLINE / 2}
			width={cut.window.size - OUTLINE}
			height={cut.window.size - OUTLINE}
			rx={cut.window.rx - OUTLINE / 2}
			stroke-width={OUTLINE}
		/>
	{:else}
		<rect class="win" x={cut.window.x} y={cut.window.y} width={cut.window.size} height={cut.window.size} rx={cut.window.rx} />
	{/if}
</svg>

<style>
	.mark {
		display: block;
		flex: none;
		overflow: visible;
	}
	.house {
		fill: var(--mark-house);
	}
	.win {
		fill: var(--mark-window);
		transform-box: fill-box;
		transform-origin: 50% 50%;
	}
	.win-outline {
		fill: none;
		stroke: var(--mark-offline);
		opacity: 0.45;
	}
	/* As the kit reference (uebersicht.html): 0.82 <-> 1, 1.6 s each way, ease-in-out. */
	.loading .win {
		animation: mark-breathe 1.6s ease-in-out infinite alternate;
	}
	@keyframes mark-breathe {
		from {
			transform: scale(0.82);
		}
		to {
			transform: scale(1);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.loading .win {
			animation: none;
		}
	}
</style>
