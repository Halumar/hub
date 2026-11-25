<script>
	/**
	 * Inline "could not load, value unknown" state for a settings card: the
	 * card shows this instead of silently falling back to defaults.
	 */
	import Banner from '$lib/ui/Banner.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	let { message = '', onretry = null } = $props();
</script>

<Banner tone="error">
	{t('common.couldNotLoadSetting', { detail: message ? `: ${message}` : '' })}
	{#snippet actions()}
		{#if onretry}<button type="button" class="btn btn-sm" onclick={onretry}>{t('common.retry')}</button>{/if}
	{/snippet}
</Banner>
