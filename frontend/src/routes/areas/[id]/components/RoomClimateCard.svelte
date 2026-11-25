<script>
	/**
	 * Room climate of the area (contract docs/room-climate.md): which entity gives the room
	 * temperature and humidity of the area card, the climate capsule, the wall panel and widgets.
	 * "Automatic" = the rule (thermostats before sensors, fridge/outdoor/pool … left out); the admin
	 * may pick one placed entity per measure. Saved at once.
	 */
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t, fmtNumber } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';

	let { areaId, version = 0 } = $props();

	const MEASURES = ['temperature', 'humidity'];
	let data = $state(null);
	let loadError = $state('');
	let saving = $state(false);

	$effect(() => {
		version;
		load(areaId);
	});

	async function load(id) {
		loadError = '';
		try {
			const res = await api.getRoomClimate(id);
			data = MEASURES.every((m) => Array.isArray(res?.[m]?.options)) ? res : null;
			if (!data) loadError = '?';
		} catch (err) {
			loadError = err?.message || String(err);
		}
	}

	function names(setting, ids) {
		const byId = new Map(setting.options.map((o) => [o.entity_id, o.name]));
		return ids.map((id) => byId.get(id) || id).join(', ');
	}

	function autoLabel(setting) {
		if (setting.entity_id || !setting.pick.entity_ids.length) return t('editor.roomClimate.auto');
		return t('editor.roomClimate.autoWith', { names: names(setting, setting.pick.entity_ids) });
	}

	function optionLabel(o) {
		const kind = o.tier === 'device' ? ` · ${t('editor.roomClimate.thermostat')}` : '';
		const why = o.excluded ? ` – ${t('editor.roomClimate.notCounted', { reason: t(`editor.roomClimate.reasons.${o.excluded}`) })}` : '';
		return `${o.name}${kind}${why}`;
	}

	function shown(setting, measure) {
		if (setting.value === null || setting.value === undefined) return t('editor.roomClimate.showsNothing');
		const unit = setting.unit || (measure === 'humidity' ? '%' : '°');
		const value = `${fmtNumber(Math.round(setting.value * 10) / 10)} ${unit}`;
		const count = setting.pick.source === 'auto' ? setting.pick.entity_ids.length : 1;
		const median = count > 1 ? ` (${t('editor.roomClimate.median', { count })})` : '';
		return t('editor.roomClimate.shows', { value }) + median;
	}

	async function choose(measure, value) {
		if (!data) return;
		const body = {
			temperature_entity_id: data.temperature.entity_id,
			humidity_entity_id: data.humidity.entity_id,
			[`${measure}_entity_id`]: value || null,
		};
		saving = true;
		try {
			const res = await api.setRoomClimate(areaId, body);
			if (MEASURES.every((m) => Array.isArray(res?.[m]?.options))) data = res;
			else await load(areaId);
			addToast(t('editor.roomClimate.saved'), 'success');
		} catch (err) {
			addToast(t('editor.roomClimate.failed', { message: err?.message || String(err) }), 'error');
			await load(areaId);
		} finally {
			saving = false;
		}
	}
</script>

<section class="rc" aria-labelledby="rc-title" data-testid="room-climate">
	<h2 id="rc-title" class="rc-title"><Icon name="thermo" />{t('editor.roomClimate.title')}</h2>
	{#if loadError}
		<p class="t-help" role="status">{t('editor.roomClimate.loadFailed', { message: loadError })}</p>
	{:else if !data}
		<p class="t-help">{t('common.loading')}</p>
	{:else}
		{#each MEASURES as m (m)}
			{@const s = data[m]}
			<div class="rc-row">
				<label class="t-label" for="rc-{m}">{t(`editor.roomClimate.${m}`)}</label>
				{#if s.options.length}
					<select
						id="rc-{m}"
						class="select"
						value={s.entity_id || ''}
						disabled={saving}
						onchange={(e) => choose(m, e.currentTarget.value)}
						data-testid="room-climate-{m}"
					>
						<option value="">{autoLabel(s)}</option>
						{#each s.options as o (o.entity_id)}
							<option value={o.entity_id}>{optionLabel(o)}</option>
						{/each}
					</select>
					<span class="t-help" data-testid="room-climate-{m}-value">{shown(s, m)}</span>
				{:else}
					<span class="t-help" data-testid="room-climate-{m}-value">{t('editor.roomClimate.none')}</span>
				{/if}
			</div>
		{/each}
		<p class="t-help">{t('editor.roomClimate.help')}</p>
	{/if}
</section>

<style>
	.rc {
		background: var(--surface);
		border: 1px solid var(--line);
		border-radius: var(--r-card);
		padding: 12px;
		display: grid;
		gap: 10px;
	}
	.rc-title {
		display: flex;
		align-items: center;
		gap: 6px;
		margin: 0;
		font-size: var(--fs-sm);
		font-weight: 650;
	}
	.rc-row {
		display: grid;
		gap: 4px;
	}
	.rc-row .select {
		width: 100%;
		min-width: 0;
	}
</style>
