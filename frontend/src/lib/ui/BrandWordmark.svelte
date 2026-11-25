<script>
	/**
	 * The wordmark (the brand name in lower case) as inline SVG from the kit outlines (no
	 * set text): the kit's color wordmark, in dark mode its dark-background variant (--mark-* tokens).
	 * The h of the wordmark is the mark, so the mark never stands beside or above it
	 * (brand rule): where a name belongs, the wordmark alone; where an icon
	 * belongs, BrandMark alone.
	 * height: rendered height in px. Keep the wordmark at least 72 px wide.
	 */
	import { BRAND } from '$lib/brand.ts';
	import { WORDMARK } from './brand-mark.js';
	let { height = 28, label = BRAND.name, class: cls = '' } = $props();
	const [, , vw, vh] = WORDMARK.viewBox.split(' ').map(Number);
</script>

<svg class="wordmark {cls}" viewBox={WORDMARK.viewBox} {height} width={(height * vw) / vh} role="img" aria-label={label} focusable="false" data-testid="brand-wordmark">
	<path class="house" d={WORDMARK.h} />
	<path class="word" d={WORDMARK.letters} />
	<rect class="win" x={WORDMARK.window.x} y={WORDMARK.window.y} width={WORDMARK.window.size} height={WORDMARK.window.size} rx={WORDMARK.window.rx} />
</svg>

<style>
	.wordmark {
		display: block;
		flex: none;
		overflow: visible;
	}
	.house {
		fill: var(--mark-house);
	}
	.win {
		fill: var(--mark-window);
	}
	.word {
		fill: var(--mark-word);
	}
</style>
