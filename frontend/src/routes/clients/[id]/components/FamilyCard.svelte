<script>
	/**
	 * Family manager of a device: whether the effective rule
	 * `family_manager` is on (granted by the profile or an adjustment –
	 * "Adjust the right" opens the normal adjust dialog), which children it
	 * looks after (all, also later ones, or a selection) and its HA notify
	 * targets (answer buttons for the companion app). Pro hint: the app
	 * needs Pro on this device; the hub only shows it.
	 */
	import { onMount, untrack } from 'svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { L, loadCatalog, ruleOf } from '$lib/profiles/catalog.svelte.js';
	import { errorText } from '$lib/profiles/errors.js';
	import Icon from '$lib/ui/Icon.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import Banner from '$lib/ui/Banner.svelte';

	let { client, effective, onadjust = null } = $props();

	const TARGET_RE = /^notify\.[a-z0-9_]{1,64}$/;

	let family = $state(null);
	let allChildren = $state([]);
	let mode = $state('all');
	let selected = $state([]);
	let targets = $state([]);
	let newTarget = $state('');
	let targetError = $state('');
	let busy = $state(false);
	let error = $state('');

	let on = $derived(effective?.rules?.family_manager?.value === true);
	let cid = $derived(client?.id);

	onMount(() => {
		loadCatalog().catch(() => {});
		const offs = [
			onBroadcast('family_changed', (d) => {
				if (!d?.client_ids?.length || d.client_ids.includes(client?.id)) load();
			}),
			onBroadcast('client_paired', () => load()),
		];
		return () => offs.forEach((off) => off());
	});

	$effect(() => {
		if (cid) untrack(load);
	});

	async function load() {
		try {
			const [mine, overview] = await Promise.all([api.getClientFamily(client.id), api.getFamily()]);
			family = mine;
			allChildren = overview?.children || [];
			mode = mine.scope.all ? 'all' : 'selection';
			selected = [...(mine.scope.client_ids || [])];
			targets = [...(mine.notify_targets || [])];
			error = '';
		} catch (e) {
			error = errorText(e);
		}
	}

	function toggleChild(id) {
		selected = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
	}

	function addTarget() {
		const v = newTarget.trim();
		if (!TARGET_RE.test(v)) {
			targetError = t('family.card.targetInvalid');
			return;
		}
		if (!targets.includes(v) && targets.length < 5) targets = [...targets, v];
		newTarget = '';
		targetError = '';
	}

	async function save() {
		busy = true;
		try {
			family = await api.setClientFamily(client.id, {
				scope: { all: mode === 'all', client_ids: mode === 'all' ? [] : selected },
				notify_targets: targets,
			});
			selected = [...(family.scope.client_ids || [])];
			addToast(t('family.card.saved'), 'success');
		} catch (e) {
			addToast(t('family.saveFailed', { message: errorText(e) }), 'error');
		} finally {
			busy = false;
		}
	}

	function ruleNames(keys) {
		return keys.map((k) => L(ruleOf(k)?.label) || k).join(', ');
	}

	let shown = $derived(mode === 'all' ? allChildren : allChildren.filter((c) => selected.includes(c.client_id)));
	let fixedWarnings = $derived(shown.filter((c) => c.fixed_manageable?.length));
</script>

<section class="card" aria-labelledby="fam-h" data-testid="device-family">
	<div class="card-head">
		<Icon name="child" />
		<h2 id="fam-h" class="flex-1">{t('family.card.title')}</h2>
		<span class="badge" class:badge-ok={on} data-testid="device-family-state">{on ? t('family.card.on') : t('family.card.off')}</span>
	</div>
	{#if !on}
		<p class="t-help mb-3">{t('family.card.offHint')}</p>
		<button type="button" class="btn btn-sm" onclick={() => onadjust?.('family_manager')} disabled={!client.is_active} data-testid="device-family-grant"><Icon name="sliders" />{t('family.card.grant')}</button>
	{:else}
		<p class="t-help mb-3 pro" data-testid="device-family-pro"><Icon name="info" size="14" /><span>{t('family.card.pro')}</span></p>
	{/if}

	<span class="t-label mt-3 block" id="fam-children-l">{t('family.card.children')}</span>
	<RadioSeg
		size="even full"
		labelledby="fam-children-l"
		value={mode}
		options={[
			{ value: 'all', label: t('family.card.all') },
			{ value: 'selection', label: t('family.card.selection') },
		]}
		onchange={(m) => (mode = m)}
		testid="device-family-mode"
	/>
	{#if mode === 'all'}
		<p class="t-help mt-1">{t('family.card.allHint')}</p>
	{:else if !allChildren.length}
		<p class="t-help mt-1">{t('family.card.noChildren')}</p>
	{:else}
		<div class="kids" role="group" aria-labelledby="fam-children-l">
			{#each allChildren as c (c.client_id)}
				<label class="check">
					<input type="checkbox" checked={selected.includes(c.client_id)} disabled={c.client_id === client.id} onchange={() => toggleChild(c.client_id)} data-testid="device-family-child" />
					<span class="truncate">{c.owner_label ? `${c.owner_label} · ${c.name}` : c.name}</span>
				</label>
			{/each}
		</div>
		{#if !selected.length}<p class="t-help warnline"><Icon name="warn" size="14" />{t('family.card.noneSelected')}</p>{/if}
	{/if}
	{#each fixedWarnings as c (c.client_id)}
		<Banner tone="warn">{t('family.card.fixedWarn', { profile: c.profile_name || '', child: c.owner_label || c.name, rules: ruleNames(c.fixed_manageable) })}</Banner>
	{/each}

	<label class="t-label mt-3 block" for="fam-target">{t('family.card.targets')}</label>
	<p class="t-help">{t('family.card.targetsHint')}</p>
	{#if targets.length}
		<ul class="targets">
			{#each targets as target (target)}
				<li class="chip" data-testid="device-family-target">
					<span class="truncate">{target}</span>
					<button type="button" class="btn btn-ghost btn-icon btn-sm" onclick={() => (targets = targets.filter((x) => x !== target))} aria-label={t('family.card.removeTarget', { target })}><Icon name="x" size="14" /></button>
				</li>
			{/each}
		</ul>
	{/if}
	<div class="addrow">
		<input id="fam-target" class="input" placeholder={t('family.card.targetPlaceholder')} bind:value={newTarget} disabled={targets.length >= 5} aria-invalid={targetError ? 'true' : undefined} onkeydown={(e) => e.key === 'Enter' && (e.preventDefault(), addTarget())} data-testid="device-family-target-input" />
		<button type="button" class="btn btn-sm" onclick={addTarget} disabled={targets.length >= 5 || !newTarget.trim()} data-testid="device-family-target-add"><Icon name="plus" />{t('family.card.addTarget')}</button>
	</div>
	{#if targetError}<p class="t-err text-sm" role="alert">{targetError}</p>{/if}

	<div class="mt-3"><button type="button" class="btn btn-primary btn-sm" onclick={save} disabled={busy || !family} data-testid="device-family-save">{t('family.card.save')}</button></div>
	{#if error}<Banner tone="error">{error}</Banner>{/if}
</section>

<style>
	.kids {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 14rem), 1fr));
		gap: 6px 14px;
		margin-top: 8px;
	}
	.check {
		display: flex;
		gap: 8px;
		align-items: center;
		min-width: 0;
	}
	.targets {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin: 6px 0;
		padding: 0;
		list-style: none;
	}
	.targets .chip {
		display: inline-flex;
		gap: 4px;
		align-items: center;
		max-width: 100%;
	}
	.addrow {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 8px;
		max-width: 520px;
		margin-top: 6px;
	}
	.pro,
	.warnline {
		display: flex;
		gap: 6px;
		align-items: flex-start;
	}
</style>
