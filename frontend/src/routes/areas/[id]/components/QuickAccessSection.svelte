<script>
	// Quick access of one entity: widgets, controls and watches on iPhone, iPad and Apple Watch
	// (Control Center, CarPlay, Siri) and on Android (widgets, quick settings tiles, device
	// controls).
	// Saved at once (independent of the tile draft): the level applies to the entity in every
	// area. Critical entities need the confirmed warning for "control".
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Banner from '$lib/ui/Banner.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';

	let { entityId, name = '' } = $props();

	const ORDER = ['off', 'status', 'control'];
	let item = $state(null);
	let loadError = $state('');
	let saving = $state(false);
	let pendingControl = $state(false);
	let ack = $state(false);
	let fieldErr = $state('');

	const safe = $derived(entityId.startsWith('lock.') ? 'lock' : entityId.startsWith('alarm_control_panel.') ? 'arm' : null);
	const options = $derived(
		item
			? ORDER.filter((lvl) => ORDER.indexOf(lvl) <= ORDER.indexOf(item.max)).map((lvl) => ({
					value: lvl,
					label: t(`tiles.quickAccess.levels.${lvl}`),
				}))
			: []
	);
	const shown = $derived(pendingControl ? 'control' : (item?.effective ?? 'off'));

	onMount(load);

	async function load() {
		loadError = '';
		try {
			const res = await api.getQuickAccess();
			item = (res.items || []).find((i) => i.entity_id === entityId) || null;
			if (!item) loadError = t('tiles.quickAccess.unknown');
		} catch (err) {
			loadError = err?.message || String(err);
		}
	}

	async function put(level, criticalAck = false) {
		saving = true;
		fieldErr = '';
		try {
			const res = await api.setQuickAccess(entityId, { level, critical_ack: criticalAck });
			item = res.items?.[0] ?? item;
			pendingControl = false;
			ack = false;
			addToast(t('tiles.quickAccess.saved', { name }), 'success');
		} catch (err) {
			const detail = Array.isArray(err?.detail) ? err.detail[0] : null;
			fieldErr = detail?.type === 'quick_access.critical_ack_required' ? t('tiles.quickAccess.ackRequired') : err?.message || String(err);
		} finally {
			saving = false;
		}
	}

	function choose(level) {
		if (!item) return;
		if (level === 'control' && item.critical) {
			pendingControl = true;
			return;
		}
		pendingControl = false;
		put(level === item.default ? null : level);
	}
</script>

<div class="grp" data-testid="tile-quick-access">
	<h3 id="qa-title-{entityId}">{t('tiles.quickAccess.title')}</h3>
	{#if loadError}
		<p class="t-help" role="status">{loadError}</p>
	{:else if !item}
		<p class="t-help">{t('common.loading')}</p>
	{:else if item.max === 'off'}
		<p class="t-help" data-testid="qa-never">{t('tiles.quickAccess.never')}</p>
	{:else}
		<RadioSeg
			size="seg-sm"
			label={t('tiles.quickAccess.title')}
			options={options}
			value={shown}
			onchange={choose}
			testid="qa-level"
		/>
		<p class="t-help">
			{t(`tiles.quickAccess.explain.${shown}`)}
			{#if item.level === null}· {t('tiles.quickAccess.isDefault')}{/if}
		</p>
		{#if item.critical}
			{#if pendingControl}
				<Banner tone="warn" live={true} testid="qa-warning">
					<p>{t('tiles.quickAccess.warning', { name })}</p>
					{#if safe}<p>{t(`tiles.quickAccess.warningSafe.${safe}`)}</p>{/if}
					<label class="chk mt-2">
						<input type="checkbox" bind:checked={ack} data-testid="qa-ack" />
						{t('tiles.quickAccess.ack')}
					</label>
					<div class="row mt-2">
						<button type="button" class="btn btn-sm" onclick={() => (pendingControl = false)} disabled={saving}>{t('common.cancel')}</button>
						<button type="button" class="btn btn-primary btn-sm" onclick={() => put('control', true)} disabled={!ack || saving} data-testid="qa-confirm">
							{t('tiles.quickAccess.allow')}
						</button>
					</div>
				</Banner>
			{:else}
				<p class="t-help">{t('tiles.quickAccess.critical')}{#if safe} {t(`tiles.quickAccess.warningSafe.${safe}`)}{/if}</p>
			{/if}
		{/if}
		{#if fieldErr}<p class="field-error" role="alert">{fieldErr}</p>{/if}
		<p class="t-help">{t('tiles.quickAccess.everywhere')}</p>
	{/if}
</div>

<style>
	.grp {
		display: grid;
		gap: 10px;
	}
	.grp h3 {
		margin: 0;
		font-size: var(--fs-cap);
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--ink-3);
	}
	.row {
		display: flex;
		gap: 8px;
		justify-content: flex-end;
	}
	.chk {
		display: flex;
		gap: 8px;
		align-items: center;
	}
</style>
