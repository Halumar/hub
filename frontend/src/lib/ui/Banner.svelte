<script>
	/**
	 * Inline banner. tone: info | ok | warn | error | neutral.
	 * error/warn are announced (role=alert / status), info is static.
	 *
	 *   <Banner tone="warn" title="…">text {#snippet actions()}<button class="btn btn-sm">…</button>{/snippet}</Banner>
	 */
	import Icon from './Icon.svelte';

	let { tone = 'info', title = '', icon = '', testid = undefined, live = true, children = null, actions = null, class: cls = '' } = $props();

	const ICON = { info: 'info', ok: 'checkcircle', warn: 'warn', error: 'error', neutral: 'info' };
	let role = $derived(!live ? undefined : tone === 'error' ? 'alert' : tone === 'warn' ? 'status' : undefined);
</script>

<div class="banner banner-{tone} {cls}" {role} data-testid={testid}>
	<Icon name={icon || ICON[tone] || 'info'} />
	<div class="banner-body">
		{#if title}<b>{title}</b>{#if children}{' '}{/if}{/if}
		{@render children?.()}
	</div>
	{#if actions}<div class="banner-actions">{@render actions()}</div>{/if}
</div>
