<script>
	/** Loading state for the first load of a page or card (later reloads are silent). */
	import { t } from '$lib/i18n/index.svelte.js';
	import BrandMark from './BrandMark.svelte';
	let { label = '', inline = false } = $props();
</script>

{#if inline}
	<span class="inline-flex items-center gap-2 text-ink-2" role="status">
		<span class="spinner spinner-sm" aria-hidden="true"></span>{label || t('common.loading')}
	</span>
{:else}
	<div class="state" role="status" data-testid="loading">
		<BrandMark size={40} state="loading" />
		<p>{label || t('common.loading')}</p>
	</div>
{/if}
