<script>
	/** Error state with retry (never shown together with an empty state). */
	import Icon from './Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	let { title = '', message = '', onretry = null, testid = undefined } = $props();
</script>

<div class="state" role="alert" data-testid={testid}>
	<span class="text-err-ink"><Icon name="error" /></span>
	<h3>{title || t('common.error')}</h3>
	{#if message}<p>{message}</p>{/if}
	{#if onretry}
		<button type="button" class="btn mt-2" onclick={onretry}><Icon name="refresh" />{t('common.retry')}</button>
	{/if}
</div>
