<script>
	/** Empty state: icon, title, text and an optional action. */
	import Icon from './Icon.svelte';
	let { icon = 'info', title, text = '', testid = undefined, children = null, compact = false } = $props();
</script>

<div class="state {compact ? 'py-6' : ''}" data-testid={testid}>
	<Icon name={icon} />
	<h3>{title}</h3>
	{#if text}<p>{text}</p>{/if}
	{#if children}<div class="row justify-center mt-2">{@render children()}</div>{/if}
</div>
