<script>
	/** Calm admin hint when Home Assistant is reached over http. */
	import { t } from '$lib/i18n/index.svelte.js';
	import { BRAND } from '$lib/brand.ts';
	import Banner from '$lib/ui/Banner.svelte';
	import { haCleartextUrl, connectionDocsUrl } from '$lib/ha-cleartext.js';

	let { haUrls = null, class: cls = '' } = $props();
	let url = $derived(haCleartextUrl(haUrls));
</script>

{#if url}
	<Banner tone="info" live={false} class={cls} testid="ha-cleartext-hint">
		{t('settings.haCleartext')}
		{#snippet actions()}
			<a class="btn btn-sm" href={connectionDocsUrl(BRAND.urls.website)} target="_blank" rel="noopener noreferrer">{t('settings.haCleartextLink')}</a>
		{/snippet}
	</Banner>
{/if}
