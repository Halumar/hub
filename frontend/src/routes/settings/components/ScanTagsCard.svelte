<script>
	/**
	 * NFC and QR tags of this hub: a tag carries only its link
	 * https://<website>/t/<tag_id>; what it does is stored here. Devices resolve
	 * it at the hub and switch like a widget (quick access level, profile,
	 * control path). Pro is checked by the app.
	 */
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { copyText, formatRelativeDate } from '$lib/utils.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Field from '$lib/ui/Field.svelte';
	import LoadError from '$lib/components/LoadError.svelte';

	const KINDS = ['show', 'toggle', 'turn_on', 'turn_off', 'run'];
	const ERRORS = ['invalid_target', 'tag_exists', 'tag_limit', 'invalid_name'];

	let tags = $state([]);
	let max = $state(100);
	let loadError = $state('');
	let areas = $state([]);
	let entities = $state([]);
	let name = $state('');
	let kind = $state('toggle');
	let target = $state('');
	let busy = $state(false);
	let formError = $state('');

	// Targets: areas only for "show" (open an area), entities for every kind.
	let areaTargets = $derived(kind === 'show' ? areas.map((a) => ({ value: `area:${a.id}`, label: `${t('settings.tags.area')}: ${a.name}` })) : []);

	onMount(load);

	async function load() {
		loadError = '';
		try {
			const r = await api.getScanTags();
			tags = r.tags || [];
			max = r.max || 100;
		} catch (e) {
			loadError = e?.message || t('settings.tags.loadFailed');
		}
		try {
			const a = await api.getAreas();
			areas = (Array.isArray(a) ? a : a?.areas || []).map((x) => ({ id: x.id, name: x.name }));
			const ents = await api.getHAEntities();
			entities = (Array.isArray(ents) ? ents : ents?.entities || []).map((e) => e.entity_id).filter(Boolean).sort();
		} catch {
			/* optional pickers */
		}
	}

	function actionOf() {
		if (target.startsWith('area:')) return { kind: 'show', area_id: target.slice(5) };
		return { kind, entity_id: target.trim() || null };
	}

	function describe(tag) {
		const a = tag.action || {};
		const what = a.area_id ? `${t('settings.tags.area')}: ${areas.find((x) => x.id === a.area_id)?.name || a.area_id}` : a.entity_id || '–';
		return `${t(`settings.tags.kind.${a.kind}`)} · ${what}`;
	}

	async function add(e) {
		e.preventDefault();
		formError = '';
		busy = true;
		try {
			await api.createScanTag({ name: name.trim(), action: actionOf() });
			name = '';
			target = '';
			addToast(t('settings.tags.added'), 'success');
			await load();
		} catch (err) {
			const code = err?.code;
			formError = code && ERRORS.includes(code) ? t(`settings.tags.error.${code}`) : err?.message || t('settings.tags.failed');
		} finally {
			busy = false;
		}
	}

	async function remove(tag) {
		busy = true;
		try {
			await api.deleteScanTag(tag.tag_id);
			addToast(t('settings.tags.removed', { name: tag.name }), 'success');
			await load();
		} catch (err) {
			addToast(err?.message || t('settings.tags.failed'), 'error');
		} finally {
			busy = false;
		}
	}

	async function copy(tag) {
		await copyText(tag.url);
		addToast(t('settings.tags.copied'), 'success');
	}
</script>

<section class="card" aria-labelledby="tags-h" data-testid="scan-tags-card">
	<div class="card-head">
		<Icon name="tag" /><h2 id="tags-h" class="flex-1">{t('settings.tags.title')}</h2>
		<span class="badge">{t('settings.tags.pro')}</span>
	</div>
	<p class="t-help mb-3">{t('settings.tags.hint')}</p>
	{#if loadError}
		<LoadError message={loadError} onretry={load} />
	{:else}
		{#if tags.length}
			<ul class="list" data-testid="scan-tags-list">
				{#each tags as tag (tag.tag_id)}
					<li>
						<div class="flex-1 min-w-0">
							<div class="truncate"><b>{tag.name}</b></div>
							<div class="t-help truncate">{describe(tag)}{#if tag.last_scanned_at} · {t('settings.tags.lastScan', { when: formatRelativeDate(tag.last_scanned_at) })}{/if}</div>
							<code class="t-mono t-help break-all">{tag.url}</code>
						</div>
						<button type="button" class="btn btn-ghost btn-sm" onclick={() => copy(tag)} aria-label={t('settings.tags.copy', { name: tag.name })}><Icon name="copy" size="14" /></button>
						<button type="button" class="btn btn-ghost btn-sm" disabled={busy} onclick={() => remove(tag)} aria-label={t('settings.tags.remove', { name: tag.name })} data-testid="scan-tag-remove"><Icon name="trash" size="14" /></button>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="t-help" data-testid="scan-tags-empty">{t('settings.tags.empty')}</p>
		{/if}

		{#if tags.length < max}
			<form class="add mt-3" onsubmit={add}>
				<Field label={t('settings.tags.name')} required>
					{#snippet children({ id, describedby, invalid })}
						<input {id} class="input" maxlength="60" required aria-describedby={describedby} aria-invalid={invalid} bind:value={name} data-testid="scan-tag-name" />
					{/snippet}
				</Field>
				<Field label={t('settings.tags.action')}>
					{#snippet children({ id })}
						<select {id} class="select" bind:value={kind} onchange={() => target.startsWith('area:') && kind !== 'show' && (target = '')} data-testid="scan-tag-kind">
							{#each KINDS as k (k)}<option value={k}>{t(`settings.tags.kind.${k}`)}</option>{/each}
						</select>
					{/snippet}
				</Field>
				<Field label={t('settings.tags.target')} required error={formError}>
					{#snippet children({ id, describedby, invalid })}
						{#if kind === 'show' && areaTargets.length}
							<select {id} class="select" bind:value={target} aria-describedby={describedby} aria-invalid={invalid} data-testid="scan-tag-target-select">
								<option value="">{t('settings.tags.chooseEntity')}</option>
								{#each areaTargets as a (a.value)}<option value={a.value}>{a.label}</option>{/each}
							</select>
						{/if}
						{#if !target.startsWith('area:')}
							<input {id} class="input mt-1" list="scan-tag-entities" placeholder="light.flur" required aria-describedby={describedby} aria-invalid={invalid} bind:value={target} data-testid="scan-tag-target" />
							<datalist id="scan-tag-entities">{#each entities as eid (eid)}<option value={eid}></option>{/each}</datalist>
						{/if}
					{/snippet}
				</Field>
				<div><button type="submit" class="btn btn-primary" disabled={busy || !name.trim() || !target.trim()} data-testid="scan-tag-add">{t('settings.tags.add')}</button></div>
			</form>
		{/if}
	{/if}
</section>

<style>
	.add {
		display: grid;
		gap: 10px;
	}
</style>
