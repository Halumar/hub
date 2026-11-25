<script>
	/**
	 * Stroke icon from $lib/ui/icons.js. Decorative by default (aria-hidden);
	 * pass `label` only when the icon alone carries meaning.
	 */
	import { ICONS } from './icons.js';

	let { name, label = '', class: cls = '', size = null } = $props();
</script>

<svg
	class="ic {cls}"
	viewBox="0 0 24 24"
	fill="none"
	stroke="currentColor"
	stroke-width="1.8"
	stroke-linecap="round"
	stroke-linejoin="round"
	width={size}
	height={size}
	aria-hidden={label ? undefined : 'true'}
	role={label ? 'img' : undefined}
	aria-label={label || undefined}
	focusable="false"
>{@html ICONS[name] || ICONS.info}</svg>
