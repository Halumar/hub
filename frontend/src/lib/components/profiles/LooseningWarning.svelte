<script>
	/**
	 * Confirmation when a write loosens security rules (catalog
	 * loosening_warning): the warning texts plus a checkbox the admin
	 * ticks before saving. No mandatory reason.
	 */
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { L, ruleOf } from '$lib/profiles/catalog.svelte.js';

	let { rules = [], confirmed = $bindable(false), subject = '' } = $props();

	const uid = `lw${Math.random().toString(36).slice(2, 8)}`;
</script>

{#if rules.length}
	<div class="banner banner-warn lw" role="status" data-testid="loosening-warning">
		<Icon name="warn" />
		<div class="banner-body">
			<b>{t('profiles.loosen.title', { count: rules.length })}</b>
			<ul>
				{#each rules as key (key)}
					{@const r = ruleOf(key)}
					<li><b>{L(r?.label) || key}:</b> {r?.loosening_warning ? L(r.loosening_warning, { device: subject }) : t('profiles.loosen.generic')}</li>
				{/each}
			</ul>
			<label class="check" for={uid}>
				<input id={uid} type="checkbox" bind:checked={confirmed} data-testid="loosening-confirm" />
				{t('profiles.loosen.confirm')}
			</label>
		</div>
	</div>
{/if}

<style>
	.lw ul {
		margin: 4px 0 6px;
		padding-left: 18px;
	}
</style>
