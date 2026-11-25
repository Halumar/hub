<script>
	/**
	 * Kids rule "Exceptions for areas and devices" (kids_exceptions):
	 * other kids rules for an area, a device (entity) or the device's own
	 * area. Always fixed in the profile. Each exception is edited with the
	 * kids rule editor (inherits from the profile's kids rules).
	 */
	import Modal from '$lib/ui/Modal.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import RuleEditor from '$lib/components/kids/RuleEditor.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { RULE_KEYS, summarize, FALLBACK_TYPES } from '$lib/data/kids.js';
	import { clone } from '$lib/profiles/format.js';

	let { value = [], ctx = {}, disabled = false, onchange } = $props();

	let open = $state(false);
	let editIndex = $state(-1);
	let targetKind = $state('own_area');
	let targetId = $state('');
	let draftRules = $state({});

	let list = $derived(Array.isArray(value) ? value : []);

	function targetLabel(target) {
		if (target?.own_area) return t('profiles.v.ownArea');
		if (target?.area_id) return (ctx.areas || []).find((a) => a.id === target.area_id)?.name || t('profiles.exc.areaDeleted');
		if (target?.entity_id) return (ctx.entities || []).find((e) => e.entity_id === target.entity_id)?.friendly_name || target.entity_id;
		return '–';
	}

	function rulesSummary(rules) {
		return RULE_KEYS.filter((k) => rules && k in rules)
			.map((k) => `${t(`kids.block.${k}`)}: ${summarize(k, rules[k])}`)
			.join(' · ');
	}

	function startAdd() {
		editIndex = -1;
		targetKind = 'own_area';
		targetId = '';
		draftRules = {};
		open = true;
	}

	function startEdit(i) {
		const exc = list[i];
		editIndex = i;
		targetKind = exc.target.own_area ? 'own_area' : exc.target.area_id ? 'area' : 'entity';
		targetId = exc.target.area_id || exc.target.entity_id || '';
		draftRules = clone(exc.rules) || {};
		open = true;
	}

	function remove(i) {
		onchange?.(list.filter((_, j) => j !== i));
	}

	function target() {
		if (targetKind === 'own_area') return { own_area: true };
		if (targetKind === 'area') return { area_id: targetId };
		return { entity_id: targetId };
	}

	async function saveRules(rules) {
		const exc = { target: target(), rules };
		const next = [...list];
		if (editIndex >= 0) next[editIndex] = exc;
		else next.push(exc);
		onchange?.(next);
		open = false;
	}

	let targetReady = $derived(targetKind === 'own_area' || !!targetId);
	let level = $derived(targetKind === 'entity' ? 'entity' : 'area');
</script>

<div class="exc" data-testid="kids-exceptions">
	{#if !list.length}
		<p class="t-help">{t('profiles.v.noExceptions')}</p>
	{/if}
	{#each list as exc, i (i)}
		<div class="box item" data-testid="kids-exception">
			<Icon name={exc.target.entity_id ? 'bulb' : 'grid'} />
			<span class="min-w-0 flex-1">
				<b class="block truncate">{targetLabel(exc.target)}</b>
				<small class="t-help">{rulesSummary(exc.rules) || t('profiles.exc.noRules')}</small>
			</span>
			<button type="button" class="btn btn-ghost btn-icon" {disabled} onclick={() => startEdit(i)} aria-label={t('profiles.exc.edit', { name: targetLabel(exc.target) })}><Icon name="edit" /></button>
			<button type="button" class="btn btn-ghost btn-icon" {disabled} onclick={() => remove(i)} aria-label={t('profiles.exc.remove', { name: targetLabel(exc.target) })}><Icon name="trash" /></button>
		</div>
	{/each}
	<div><button type="button" class="btn btn-sm" {disabled} onclick={startAdd} data-testid="kids-exception-add"><Icon name="plus" />{t('profiles.exc.add')}</button></div>
</div>

<Modal bind:open title={editIndex >= 0 ? t('profiles.exc.editTitle') : t('profiles.exc.addTitle')} description={t('profiles.exc.hint')} size="lg" testid="kids-exception-dialog">
	<div class="stack">
		<RadioSeg
			size="even full"
			label={t('profiles.exc.target')}
			value={targetKind}
			options={[
				{ value: 'own_area', label: t('profiles.v.ownArea') },
				{ value: 'area', label: t('profiles.v.area') },
				{ value: 'entity', label: t('profiles.exc.entity') },
			]}
			onchange={(k) => ((targetKind = k), (targetId = ''))}
		/>
		{#if targetKind === 'area'}
			<label class="field"><span class="field-label">{t('profiles.v.area')}</span>
				<select class="select" bind:value={targetId} data-testid="exc-area">
					<option value="">{t('profiles.exc.pick')}</option>
					{#each ctx.areas || [] as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
				</select>
			</label>
		{:else if targetKind === 'entity'}
			<label class="field"><span class="field-label">{t('profiles.exc.entity')}</span>
				<select class="select" bind:value={targetId}>
					<option value="">{t('profiles.exc.pick')}</option>
					{#each ctx.entities || [] as e (e.entity_id)}<option value={e.entity_id}>{e.friendly_name || e.entity_id}</option>{/each}
				</select>
			</label>
		{/if}
		{#if targetReady}
			<RuleEditor {level} rules={draftRules} inherited={ctx.kidsRules || null} types={FALLBACK_TYPES} onsave={saveRules} oncancel={() => (open = false)} idPrefix="exc" testid="exception-rules" />
		{/if}
	</div>
</Modal>

<style>
	.exc {
		display: grid;
		gap: 8px;
		min-width: 0;
	}
	.item {
		display: flex;
		gap: 10px;
		align-items: center;
		min-width: 0;
	}
</style>
