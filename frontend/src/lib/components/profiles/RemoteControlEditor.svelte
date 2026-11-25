<script>
	/**
	 * "When away": level per device type and per device while the
	 * device is connected from away – operate, show only, hide. A standard
	 * level plus exceptions; entity beats device type beats standard.
	 * Important alerts (alarm, smoke, water, gas) are never hidden: at most
	 * "show only".
	 */
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { REMOTE_LEVELS, REMOTE_TYPES, safetyTypes, deviceTypeLabel, levelLabel } from '$lib/profiles/format.js';

	let { value, ctx = {}, disabled = false, idPrefix = 'rc', onchange } = $props();

	let addType = $state('');
	let addEntity = $state('');

	let v = $derived(value || { default: 'control', domains: {}, entities: {} });
	let domainKeys = $derived(Object.keys(v.domains || {}));
	let entityKeys = $derived(Object.keys(v.entities || {}));
	let freeTypes = $derived(REMOTE_TYPES.filter((x) => !domainKeys.includes(x)));
	let freeEntities = $derived((ctx.entities || []).filter((e) => !entityKeys.includes(e.entity_id)));

	function isSafety(type) {
		return safetyTypes().includes(type) || safetyTypes().includes(String(type).split('.')[0]);
	}

	function entitySafety(entityId) {
		const e = (ctx.entities || []).find((x) => x.entity_id === entityId);
		const domain = entityId.split('.')[0];
		return domain === 'alarm_control_panel' || (domain === 'binary_sensor' && safetyTypes().includes(`binary_sensor:${e?.device_class}`));
	}

	function opts(safe) {
		return REMOTE_LEVELS.map((l) => ({ value: l, label: levelLabel(l), disabled: safe && l === 'hidden', title: safe && l === 'hidden' ? t('profiles.safety.neverHidden') : undefined }));
	}

	function emit(next) {
		onchange?.(next);
	}

	function setDefault(l) {
		emit({ ...v, default: l });
	}

	function setDomain(type, l) {
		emit({ ...v, domains: { ...v.domains, [type]: l } });
	}

	function removeDomain(type) {
		const d = { ...v.domains };
		delete d[type];
		emit({ ...v, domains: d });
	}

	function setEntity(id, l) {
		emit({ ...v, entities: { ...v.entities, [id]: l } });
	}

	function removeEntity(id) {
		const d = { ...v.entities };
		delete d[id];
		emit({ ...v, entities: d });
	}

	function addDomain() {
		if (!addType) return;
		setDomain(addType, isSafety(addType) ? 'view' : v.default === 'control' ? 'view' : 'control');
		addType = '';
	}

	function addEnt() {
		if (!addEntity) return;
		setEntity(addEntity, entitySafety(addEntity) ? 'view' : 'view');
		addEntity = '';
	}

	function entityName(id) {
		return (ctx.entities || []).find((e) => e.entity_id === id)?.friendly_name || id;
	}
</script>

<div class="rc" data-testid="remote-control-editor">
	<div class="line">
		<span class="lbl"><b>{t('profiles.remote.standard')}</b><small class="t-help">{t('profiles.remote.standardHint')}</small></span>
		<RadioSeg size="even full" label={t('profiles.remote.standard')} value={v.default} options={opts(false)} onchange={setDefault} testid="{idPrefix}-rc-default" />
	</div>

	{#if domainKeys.length}
		<h4 class="t-label">{t('profiles.remote.types')}</h4>
		{#each domainKeys as type (type)}
			<div class="line" data-testid="rc-type-{type}">
				<span class="lbl">
					<span class="truncate">{deviceTypeLabel(type)}</span>
					{#if isSafety(type)}<small class="t-help">{t('profiles.safety.atMostView')}</small>{/if}
				</span>
				<div class="ctl">
					<RadioSeg size="even full" label={t('profiles.remote.levelFor', { name: deviceTypeLabel(type) })} value={v.domains[type]} options={opts(isSafety(type))} onchange={(l) => setDomain(type, l)} />
					<button type="button" class="btn btn-ghost btn-icon" {disabled} onclick={() => removeDomain(type)} aria-label={t('profiles.remote.removeType', { name: deviceTypeLabel(type) })}><Icon name="trash" /></button>
				</div>
			</div>
		{/each}
	{/if}

	{#if entityKeys.length}
		<h4 class="t-label">{t('profiles.remote.entities')}</h4>
		{#each entityKeys as id (id)}
			<div class="line">
				<span class="lbl"><span class="truncate">{entityName(id)}</span><small class="t-help t-mono truncate">{id}</small></span>
				<div class="ctl">
					<RadioSeg size="even full" label={t('profiles.remote.levelFor', { name: entityName(id) })} value={v.entities[id]} options={opts(entitySafety(id))} onchange={(l) => setEntity(id, l)} />
					<button type="button" class="btn btn-ghost btn-icon" {disabled} onclick={() => removeEntity(id)} aria-label={t('profiles.remote.removeType', { name: entityName(id) })}><Icon name="trash" /></button>
				</div>
			</div>
		{/each}
	{/if}

	<div class="adds">
		<div class="add">
			<label class="sr-only" for="{idPrefix}-rc-addtype">{t('profiles.remote.addType')}</label>
			<select id="{idPrefix}-rc-addtype" class="select" bind:value={addType} {disabled}>
				<option value="">{t('profiles.remote.addType')}</option>
				{#each freeTypes as type (type)}<option value={type}>{deviceTypeLabel(type)}</option>{/each}
			</select>
			<button type="button" class="btn" onclick={addDomain} disabled={disabled || !addType} data-testid="{idPrefix}-rc-addtype-btn"><Icon name="plus" />{t('common.add')}</button>
		</div>
		{#if ctx.entities?.length}
			<div class="add">
				<label class="sr-only" for="{idPrefix}-rc-addent">{t('profiles.remote.addEntity')}</label>
				<select id="{idPrefix}-rc-addent" class="select" bind:value={addEntity} {disabled}>
					<option value="">{t('profiles.remote.addEntity')}</option>
					{#each freeEntities as e (e.entity_id)}<option value={e.entity_id}>{e.friendly_name || e.entity_id}</option>{/each}
				</select>
				<button type="button" class="btn" onclick={addEnt} disabled={disabled || !addEntity}><Icon name="plus" />{t('common.add')}</button>
			</div>
		{/if}
	</div>
	<p class="t-help safety" data-testid="safety-note"><Icon name="shieldcheck" size="14" /><span>{t('profiles.safety.note')} {t('profiles.safety.noAlarmSystem')}</span></p>
</div>

<style>
	.rc {
		display: grid;
		gap: 8px;
		min-width: 0;
	}
	.line {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 6px;
		padding: 8px 0;
		border-bottom: 1px solid var(--line);
	}
	.lbl {
		display: grid;
		min-width: 0;
	}
	.ctl {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 6px;
		align-items: center;
		min-width: 0;
	}
	.adds {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 8px;
	}
	.add {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 6px;
	}
	.add .select {
		min-width: 0;
		width: 100%;
	}
	h4 {
		margin: 6px 0 0;
	}
	.safety {
		display: flex;
		gap: 6px;
		align-items: flex-start;
	}
	.safety :global(.ic) {
		flex: none;
		margin-top: 2px;
	}
</style>
