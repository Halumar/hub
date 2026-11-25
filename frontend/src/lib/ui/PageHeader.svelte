<script>
	/**
	 * Page header: h1 title, subtitle, optional back link and actions.
	 *
	 *   <PageHeader title="Devices" subtitle="6 devices · 5 online" back="/areas">
	 *     {#snippet actions()}<button class="btn btn-primary">…</button>{/snippet}
	 *   </PageHeader>
	 */
	import Icon from './Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	let { title, subtitle = '', back = '', backLabel = '', actions = null, below = null, testid = undefined, titleTestid = undefined } = $props();
</script>

<header class="page-head" data-testid={testid}>
	{#if back}
		<a href={back} class="btn btn-icon" aria-label={backLabel || t('common.back')}><Icon name="back" /></a>
	{/if}
	<div class="grow">
		<h1 class="t-page" data-testid={titleTestid}>{title}</h1>
		{#if subtitle}<p class="t-sub">{subtitle}</p>{/if}
	</div>
	{#if actions}<div class="row">{@render actions()}</div>{/if}
</header>
{#if below}{@render below()}{/if}
