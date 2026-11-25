<script>
	/**
	 * "Add entity" dialog with a windowed list: Home Assistant can have
	 * hundreds of entities, so only the visible rows (plus overscan) are mounted
	 * inside a full-height spacer. Search / domain filter run on the full list.
	 */
	import { t } from '$lib/i18n/index.svelte.js';
	import { domainIcon } from '$lib/data/domains.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';

	let {
		open = $bindable(false),
		sections = [],
		sectionId = $bindable(null),
		entities = [],
		allEntityIds,
		onadd,
		loading = false,
		error = '',
		onretry = null,
	} = $props();

	let search = $state('');
	let domain = $state('');
	let adding = $state('');

	let domains = $derived([...new Set(entities.map((e) => e.domain))].sort());

	let filtered = $derived.by(() => {
		const q = search.trim().toLowerCase();
		return entities.filter(
			(e) =>
				!allEntityIds.has(e.entity_id) &&
				(!domain || e.domain === domain) &&
				(!q || e.entity_id.toLowerCase().includes(q) || (e.friendly_name || '').toLowerCase().includes(q)),
		);
	});

	const ROW = 56;
	const OVERSCAN = 6;
	let scrollEl = $state(null);
	let scrollTop = $state(0);
	let viewportH = $state(400);
	let start = $derived(Math.max(0, Math.floor(scrollTop / ROW) - OVERSCAN));
	let end = $derived(Math.min(filtered.length, Math.ceil((scrollTop + viewportH) / ROW) + OVERSCAN));
	let visible = $derived(filtered.slice(start, end));

	$effect(() => {
		void search;
		void domain;
		if (scrollEl) scrollEl.scrollTop = 0;
		scrollTop = 0;
	});

	async function add(e) {
		adding = e.entity_id;
		try {
			await onadd?.(e.entity_id, e.friendly_name);
		} finally {
			adding = '';
		}
	}
</script>

<Modal bind:open title={t('editor.addEntityTitle')} size="lg" bodyClass="add-entity">
	<div class="stack">
		{#if sections.length > 1}
			<Field label={t('editor.targetSection')} id="add-entity-section">
				{#snippet children({ id })}
					<select {id} class="select" bind:value={sectionId}>
						{#each sections as s (s.id)}<option value={s.id}>{s.title || t('editor.sectionUntitled')}</option>{/each}
					</select>
				{/snippet}
			</Field>
		{/if}
		<Field label={t('editor.searchEntities')} id="add-entity-search">
			{#snippet children({ id })}
				<input {id} class="input" type="search" bind:value={search} placeholder={t('editor.searchPlaceholder')} autocomplete="off" data-autofocus />
			{/snippet}
		</Field>
		{#if domains.length > 1}
			<div class="row" role="group" aria-label={t('areas.entityTypes')}>
				<button type="button" class="chip" aria-pressed={domain === '' ? 'true' : 'false'} onclick={() => (domain = '')}>{t('editor.allDomains')}</button>
				{#each domains as d (d)}
					<button type="button" class="chip" aria-pressed={domain === d ? 'true' : 'false'} onclick={() => (domain = d)}><Icon name={domainIcon(d)} />{d}</button>
				{/each}
			</div>
		{/if}

		{#if loading}
			<LoadingState label={t('editor.loadingEntities')} />
		{:else if error}
			<ErrorState message={error} {onretry} />
		{:else if filtered.length === 0}
			<EmptyState compact icon="search" title={t('editor.noEntities')} />
		{:else}
			<p class="t-help" aria-live="polite">{t('editor.resultCount', { count: filtered.length })}</p>
			<div class="ent-scroll" bind:this={scrollEl} bind:clientHeight={viewportH} onscroll={(e) => (scrollTop = e.currentTarget.scrollTop)}>
				<ul class="ent-spacer" style:height="{filtered.length * ROW}px">
					{#each visible as e, i (e.entity_id)}
						<li class="ent-row" style:top="{(start + i) * ROW}px">
							<span class="chipic"><Icon name={domainIcon(e.entity_id)} /></span>
							<span class="ent-text">
								<b>{e.friendly_name || e.entity_id}</b>
								<small class="t-mono">{e.entity_id}{e.state ? ` · ${e.state}` : ''}</small>
							</span>
							<button type="button" class="btn btn-sm" onclick={() => add(e)} disabled={adding === e.entity_id} aria-label={t('editor.addThis', { name: e.friendly_name || e.entity_id })}>
								<Icon name="plus" />{t('common.add')}
							</button>
						</li>
					{/each}
				</ul>
			</div>
		{/if}
	</div>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close}>{t('common.close')}</button>
	{/snippet}
</Modal>

<style>
	.ent-scroll {
		height: min(420px, 50dvh);
		overflow-y: auto;
		border: 1px solid var(--line);
		border-radius: var(--r-box);
	}
	.ent-spacer {
		position: relative;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.ent-row {
		position: absolute;
		left: 0;
		right: 0;
		height: 56px;
		box-sizing: border-box;
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 0 10px;
		border-bottom: 1px solid var(--line);
	}
	.ent-text {
		flex: 1;
		min-width: 0;
	}
	.ent-text b,
	.ent-text small {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ent-text b {
		font-weight: 600;
	}
	.ent-text small {
		color: var(--ink-3);
	}
</style>
