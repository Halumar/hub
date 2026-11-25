<script>
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import IconPicker from '$lib/components/IconPicker.svelte';
	import EditAreaModal from '$lib/components/EditAreaModal.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { syncProblems } from '$lib/sync-result.js';
	import { availableDomains, domainIcon } from '$lib/data/domains.js';
	import { copyText, debounce } from '$lib/utils.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Modal from '$lib/ui/Modal.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';
	import Menu from '$lib/ui/Menu.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import RowLink from '$lib/ui/RowLink.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';

	let areas = $state([]);
	let loading = $state(true);
	let showCreateModal = $state(false);
	const EMPTY_AREA = { name: '', icon: '', description: '', color: '', grid_cols: 2, is_active: true, tags: '' };
	let newArea = $state({ ...EMPTY_AREA });
	let creating = $state(false);
	let errorMsg = $state('');
	let createError = $state('');

	// Validation state for create form
	let newAreaErrors = $state({ name: '' });

	// Delete confirmation state
	let showDeleteConfirm = $state(false);
	let areaToDelete = $state(null);
	let deleting = $state(false);
	
	// HA Import Modal state
	let showImportModal = $state(false);
	let haAreas = $state([]);
	let selectedAreaIds = $state(new Set());
	let loadingHAAreas = $state(false);
	let importIncludeEntities = $state(true);
	let syncing = $state(false);
	
	let selectedDomains = $state(new Set(availableDomains.map(d => d.id)));
	let importError = $state('');

	// Edit modal state
	let showEditModal = $state(false);
	let editArea = $state(null);

	// WS broadcast unsubscribers
	let unsubs = [];

	// Drag-to-reorder state
	let dragAreaIdx = $state(null);
	let dragOverIdx = $state(null);

	// Live events and own mutations refresh the list quietly (no spinner, the
	// scroll position stays); bursts (e.g. area_updated of a reorder) collapse.
	const refreshAreas = debounce(() => loadAreas(), 250);

	onMount(() => {
		loadAreas();
		// Real-time: refresh area list on any area change
		unsubs.push(
			onBroadcast('area_created', refreshAreas),
			onBroadcast('area_updated', refreshAreas),
			onBroadcast('area_deleted', refreshAreas),
			onBroadcast('areas_deleted', () => { areas = []; }),
			onBroadcast('entity_added', refreshAreas),
			onBroadcast('entity_removed', refreshAreas),
			onBroadcast('resync', refreshAreas),
		);
	});

	onDestroy(() => {
		refreshAreas.cancel();
		unsubs.forEach(fn => fn());
	});

	// Drag-to-reorder handlers
	function handleAreaDragStart(idx, e) {
		dragAreaIdx = idx;
		e.dataTransfer.effectAllowed = 'move';
	}

	function handleAreaDragOver(idx, e) {
		e.preventDefault();
		dragOverIdx = idx;
	}

	function handleAreaDragLeave() {
		dragOverIdx = null;
	}

	async function handleAreaDrop(idx) {
		if (dragAreaIdx === null || dragAreaIdx === idx) {
			dragAreaIdx = null;
			dragOverIdx = null;
			return;
		}
		const from = dragAreaIdx;
		dragAreaIdx = null;
		dragOverIdx = null;
		await moveArea(from, idx);
	}

	/** Move an area (drag, buttons) and store the whole order in one request. */
	async function moveArea(from, to) {
		if (to < 0 || to >= areas.length || from === to) return;
		const reordered = [...areas];
		const [moved] = reordered.splice(from, 1);
		reordered.splice(to, 0, moved);
		areas = reordered;
		try {
			await api.reorderAreas(reordered.map((a) => a.id));
			addToast(t('areas.orderSaved'), 'success');
		} catch (e) {
			addToast(t('areas.orderFailed', { message: e.message }), 'error');
			await loadAreas(); // revert to the server state
		}
	}

	function handleAreaDragEnd() {
		dragAreaIdx = null;
		dragOverIdx = null;
	}

	async function copyId(id) {
		const ok = await copyText(id);
		addToast(ok ? t('areas.idCopied') : t('common.copyFailed'), ok ? 'success' : 'error');
	}

	let loaded = false;
	/** Full-page spinner only for the very first load. */
	async function loadAreas() {
		if (!loaded) loading = true;
		try {
			areas = await api.getAreas();
			loaded = true;
			errorMsg = '';
		} catch (e) {
			errorMsg = t('areas.loadFailed', { message: e.message });
		} finally {
			loading = false;
		}
	}

	function validateNewArea() {
		newAreaErrors = { name: newArea.name.trim() ? '' : t('areas.form.nameRequired') };
		return !newAreaErrors.name;
	}

	async function createArea() {
		if (!validateNewArea()) return;
		creating = true;
		createError = '';
		try {
			const tags = newArea.tags ? newArea.tags.split(',').map(l => l.trim()).filter(Boolean) : [];
			await api.createArea({
				name: newArea.name,
				icon: newArea.icon || null,
				description: newArea.description || null,
				color: newArea.color || null,
				grid_cols: newArea.grid_cols,
				is_active: newArea.is_active,
				tags,
			});
			showCreateModal = false;
			newArea = { ...EMPTY_AREA };
			newAreaErrors = { name: '' };
			addToast(t('areas.created'), 'success');
			await loadAreas();
		} catch (e) {
			createError = t('areas.createFailed', { message: e.message });
		} finally {
			creating = false;
		}
	}

	async function toggleActive(area) {
		const next = !area.is_active;
		try {
			await api.patchArea(area.id, { is_active: next });
			addToast(t(next ? 'areas.activated' : 'areas.deactivated', { name: area.name }), 'success');
			refreshAreas();
			return true;
		} catch (e) {
			addToast(t('areas.toggleFailed', { message: e.message }), 'error');
			return false;
		}
	}

	function requestDeleteArea(area) {
		areaToDelete = area;
		showDeleteConfirm = true;
	}

	async function confirmDeleteArea() {
		if (!areaToDelete) return;
		deleting = true;
		try {
			await api.deleteArea(areaToDelete.id);
			addToast(t('areas.deleted', { name: areaToDelete.name }), 'success');
			areaToDelete = null;
			await loadAreas();
		} catch (e) {
			addToast(t('areas.deleteFailed', { message: e.message }), 'error');
		} finally {
			deleting = false;
		}
	}

	async function copyArea(area) {
		try {
			await api.copyArea(area.id);
			addToast(t('areas.copied', { name: area.name }), 'success');
			await loadAreas();
		} catch (e) {
			addToast(t('areas.copyFailed', { message: e.message }), 'error');
		}
	}

	function modeText(mode) {
		return mode ? t(`areas.modes.${mode}`) : t('areas.modes.default');
	}

	// HA Import Modal functions
	async function openImportModal() {
		loadingHAAreas = true;
		showImportModal = true;
		haAreas = [];
		importError = '';
		selectedAreaIds = new Set();
		try {
			haAreas = await api.getHAAreas();
		} catch (e) {
			importError = t('areas.importLoadFailed', { message: e.message });
		} finally {
			loadingHAAreas = false;
		}
	}

	function toggleArea(areaId) {
		const next = new Set(selectedAreaIds);
		if (next.has(areaId)) next.delete(areaId);
		else next.add(areaId);
		selectedAreaIds = next;
	}

	function toggleAll() {
		if (selectedAreaIds.size === haAreas.length) {
			selectedAreaIds = new Set();
		} else {
			selectedAreaIds = new Set(haAreas.map(a => a.area_id));
		}
	}

	function toggleDomain(domainId) {
		const next = new Set(selectedDomains);
		if (next.has(domainId)) next.delete(domainId);
		else next.add(domainId);
		selectedDomains = next;
	}

	function toggleAllDomains() {
		if (selectedDomains.size === availableDomains.length) {
			selectedDomains = new Set();
		} else {
			selectedDomains = new Set(availableDomains.map(d => d.id));
		}
	}

	async function importSelected(areaIds = null) {
		syncing = true;
		try {
			const domains = importIncludeEntities && selectedDomains.size < availableDomains.length 
				? [...selectedDomains] 
				: null;
			const result = await api.syncFromHA(areaIds, importIncludeEntities, domains);
			const params = { created: result.created, updated: result.updated, entities: result.entities_added };
			addToast(t(result.entities_added > 0 ? 'areas.importDoneEntities' : 'areas.importDone', params), 'success');
			const problems = syncProblems(result);
			if (problems) addToast(problems, 'warning', 12000);
			showImportModal = false;
			await loadAreas();
		} catch (e) {
			importError = t('areas.importFailed', { message: e.message });
		} finally {
			syncing = false;
		}
	}

	function importAllAreas() { importSelected(null); }
	function importCheckedAreas() {
		if (selectedAreaIds.size === 0) return;
		importSelected([...selectedAreaIds]);
	}

	// --- Edit Modal (shared component) ---
	function openEditModal(area) {
		editArea = area;
		showEditModal = true;
	}

	let activeCount = $derived(areas.filter((a) => a.is_active).length);

	function openCreate() {
		createError = '';
		newAreaErrors = { name: '' };
		showCreateModal = true;
	}
</script>

<div class="page">
	<PageHeader
		title={t('areas.title')}
		subtitle={loading || errorMsg || !areas.length ? '' : t('areas.subtitle', { areas: t('areas.count', { count: areas.length }), active: activeCount })}
	>
		{#snippet actions()}
			<button type="button" class="btn" onclick={openImportModal}><Icon name="download" /><span class="hidden sm:inline">{t('areas.import')}</span><span class="sm:hidden">{t('areas.importShort')}</span></button>
			<button type="button" class="btn btn-primary" onclick={openCreate} aria-label={t('areas.newArea')}><Icon name="plus" />{t('areas.newArea')}</button>
		{/snippet}
	</PageHeader>

	{#if loading}
		<LoadingState />
	{:else if errorMsg && !areas.length}
		<div class="card"><ErrorState message={errorMsg} onretry={loadAreas} /></div>
	{:else if areas.length === 0}
		<div class="card">
			<EmptyState icon="grid" title={t('areas.emptyTitle')} text={t('areas.emptyText')}>
				<button type="button" class="btn btn-primary" onclick={openCreate}><Icon name="plus" />{t('areas.createArea')}</button>
				<button type="button" class="btn" onclick={openImportModal}><Icon name="download" />{t('areas.import')}</button>
			</EmptyState>
		</div>
	{:else}
		{#if errorMsg}<Banner tone="error" class="mb-3">{errorMsg}{#snippet actions()}<button type="button" class="btn btn-sm" onclick={loadAreas}>{t('common.retry')}</button>{/snippet}</Banner>{/if}
		<ul class="area-list card card-flush" data-testid="area-list">
			{#each areas as area, idx (area.id)}
				<li
					class="area-row rowlink"
					class:inactive={!area.is_active}
					class:dragover={dragOverIdx === idx}
					class:dragging={dragAreaIdx === idx}
					data-testid="area-row"
					ondragover={(e) => handleAreaDragOver(idx, e)}
					ondragleave={handleAreaDragLeave}
					ondrop={() => handleAreaDrop(idx)}
				>
					<span
						class="drag rowlink-ctl"
						draggable="true"
						role="presentation"
						title={t('common.dragToReorder')}
						ondragstart={(e) => handleAreaDragStart(idx, e)}
						ondragend={handleAreaDragEnd}
					><Icon name="drag" /></span>
					<span class="chipic area-chip">
						<Icon name="grid" />
						{#if area.color}<i class="swatch" style:background={area.color}></i>{/if}
					</span>
					<div class="area-main">
						<h2 class="area-name"><RowLink href="{base}/areas/{area.id}" testid="area-open">{area.name}</RowLink></h2>
						<p class="area-meta">
							{t('areas.summary', { entities: t('areas.entities', { count: area.entity_count || 0 }), widgets: t('areas.widgets', { count: area.widget_count || 0 }) })}
							· {t('areas.controlModeValue', { mode: modeText(area.control_mode) })}
							{#if area.description}· {area.description}{/if}
						</p>
						<div class="row mt-1">
							{#if !area.is_active}<span class="badge badge-warn"><Icon name="eyeoff" />{t('areas.inactive')}</span>{/if}
							{#if area.ha_area_id}<span class="badge badge-info">{t('areas.fromHa')}</span>{/if}
							{#each area.tags || [] as tag (tag)}<span class="badge"><Icon name="tag" />{tag}</span>{/each}
						</div>
					</div>
					<div class="area-actions rowlink-ctl" data-testid="area-controls">
						<Switch checked={area.is_active} label={t('areas.activeShort')} onchange={() => toggleActive(area)} />
						<div class="row flex-nowrap">
							<button type="button" class="btn btn-icon btn-sm" onclick={() => moveArea(idx, idx - 1)} disabled={idx === 0} aria-label={t('common.moveUpName', { name: area.name })}><Icon name="arrowup" /></button>
							<button type="button" class="btn btn-icon btn-sm" onclick={() => moveArea(idx, idx + 1)} disabled={idx === areas.length - 1} aria-label={t('common.moveDownName', { name: area.name })}><Icon name="arrowdown" /></button>
							<Menu
								iconOnly
								ariaLabel={t('areas.actionsFor', { name: area.name })}
								items={[
									{ label: t('areas.properties'), icon: 'sliders', onclick: () => openEditModal(area), testid: 'area-properties' },
									{ label: t('areas.copy'), icon: 'copy', onclick: () => copyArea(area) },
									{ label: t('areas.copyId'), icon: 'link', onclick: () => copyId(area.id) },
									{ separator: true },
									{ label: t('areas.delete'), icon: 'trash', danger: true, onclick: () => requestDeleteArea(area), testid: 'area-delete' },
								]}
							/>
						</div>
					</div>
					<span class="rowlink-chev area-chev" aria-hidden="true"><Icon name="chev" /></span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<ConfirmDialog
	bind:open={showDeleteConfirm}
	title={t('areas.deleteTitle')}
	message={areaToDelete ? t('areas.deleteMessage', { name: areaToDelete.name }) : ''}
	confirmLabel={t('common.delete')}
	danger
	onconfirm={confirmDeleteArea}
/>

<Modal bind:open={showCreateModal} title={t('areas.createTitle')} size="md" dismissible={!creating}>
	<form id="create-area-form" class="stack" onsubmit={(e) => { e.preventDefault(); createArea(); }} novalidate>
		{#if createError}<Banner tone="error">{createError}</Banner>{/if}
		<Field label={t('areas.form.name')} required error={newAreaErrors.name} id="create-area-name">
			{#snippet children({ id, describedby, invalid })}
				<input {id} class="input" required aria-describedby={describedby} aria-invalid={invalid} bind:value={newArea.name} oninput={() => newAreaErrors.name && (newAreaErrors.name = '')} placeholder={t('areas.form.namePlaceholder')} />
			{/snippet}
		</Field>
		<Field label={t('areas.form.description')} id="create-area-description">
			{#snippet children({ id })}
				<input {id} class="input" bind:value={newArea.description} placeholder={t('areas.form.descriptionPlaceholder')} />
			{/snippet}
		</Field>
		<Field label={t('areas.form.icon')} id="create-area-icon">
			{#snippet children({ id })}
				<IconPicker {id} value={newArea.icon} onchange={(v) => (newArea.icon = v)} />
			{/snippet}
		</Field>
		<div class="field">
			<span class="field-label">{t('areas.form.grid')}</span>
			<Segmented label={t('areas.form.grid')} options={[2, 3, 4].map((n) => ({ value: n, label: String(n) }))} bind:value={newArea.grid_cols} />
		</div>
		<Field label={t('areas.form.tags')} hint={t('areas.form.tagsHint')} id="create-area-tags">
			{#snippet children({ id, describedby })}
				<input {id} class="input" aria-describedby={describedby} bind:value={newArea.tags} placeholder={t('areas.form.tagsPlaceholder')} />
			{/snippet}
		</Field>
		<Switch bind:checked={newArea.is_active} label={t('areas.form.active')} />
	</form>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={creating}>{t('common.cancel')}</button>
		<button type="submit" form="create-area-form" class="btn btn-primary" disabled={creating}>{creating ? t('common.creating') : t('areas.createArea')}</button>
	{/snippet}
</Modal>

<EditAreaModal bind:open={showEditModal} area={editArea} onSaved={refreshAreas} />

<Modal bind:open={showImportModal} title={t('areas.importTitle')} description={t('areas.importDescription')} size="lg" dismissible={!syncing}>
	{#if loadingHAAreas}
		<LoadingState label={t('areas.importLoading')} />
	{:else if importError && !haAreas.length}
		<ErrorState message={importError} onretry={openImportModal} />
	{:else if haAreas.length === 0}
		<EmptyState icon="home" title={t('areas.importNone')} />
	{:else}
		<div class="import">
			<fieldset class="stack">
				<legend class="row w-full justify-between mb-2">
					<span class="t-card">{t('areas.importAreas')}</span>
					<span class="t-help">{t('areas.importSelectedCount', { selected: selectedAreaIds.size, total: haAreas.length })}</span>
				</legend>
				<button type="button" class="btn btn-sm justify-self-start" onclick={toggleAll}>{selectedAreaIds.size === haAreas.length ? t('areas.selectNone') : t('areas.selectAll')}</button>
				<ul class="list import-list">
					{#each haAreas as ha (ha.area_id)}
						<li>
							<label class="check w-full">
								<input type="checkbox" checked={selectedAreaIds.has(ha.area_id)} onchange={() => toggleArea(ha.area_id)} />
								<span class="flex-1 min-w-0 truncate">{ha.name}</span>
							</label>
						</li>
					{/each}
				</ul>
			</fieldset>
			<div class="stack">
				<h3 class="t-card">{t('areas.importOptions')}</h3>
				<Switch bind:checked={importIncludeEntities} label={t('areas.includeEntities')} description={t('areas.includeEntitiesHint')} />
				{#if importIncludeEntities}
					<fieldset class="field">
						<legend class="field-label mb-1.5">{t('areas.entityTypes')} · {selectedDomains.size}/{availableDomains.length}</legend>
						<div class="row">
							<button type="button" class="chip" onclick={toggleAllDomains}>{selectedDomains.size === availableDomains.length ? t('areas.selectNone') : t('areas.selectAll')}</button>
							{#each availableDomains as d (d.id)}
								<button type="button" class="chip" aria-pressed={selectedDomains.has(d.id) ? 'true' : 'false'} onclick={() => toggleDomain(d.id)}>
									<Icon name={domainIcon(d.id)} />{t(`areas.domains.${d.id}`)}
								</button>
							{/each}
						</div>
					</fieldset>
				{/if}
				{#if importError}<Banner tone="error">{importError}</Banner>{/if}
			</div>
		</div>
	{/if}
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={syncing}>{t('common.cancel')}</button>
		{#if haAreas.length}
			<button type="button" class="btn" onclick={importAllAreas} disabled={syncing}>{syncing ? t('areas.importing') : t('areas.importAll', { count: haAreas.length })}</button>
			<button type="button" class="btn btn-primary" onclick={importCheckedAreas} disabled={syncing || selectedAreaIds.size === 0}>{syncing ? t('areas.importing') : t('areas.importSelected', { count: selectedAreaIds.size })}</button>
		{/if}
	{/snippet}
</Modal>

<style>
	.area-list {
		list-style: none;
		margin: 0;
	}
	.area-row {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 12px 14px;
		border-bottom: 1px solid var(--line);
	}
	.area-row:last-child {
		border-bottom: 0;
	}
	.area-row.dragover {
		box-shadow: inset 0 2px 0 var(--accent);
	}
	.area-row.dragging {
		opacity: 0.5;
	}
	.drag {
		color: var(--ink-3);
		cursor: grab;
		display: grid;
		place-items: center;
	}
	.area-chip {
		position: relative;
	}
	.swatch {
		position: absolute;
		right: -2px;
		bottom: -2px;
		width: 10px;
		height: 10px;
		border-radius: 50%;
		border: 2px solid var(--surface);
	}
	.area-main {
		flex: 1;
		min-width: 0;
	}
	.area-name {
		font-size: var(--fs-t3);
		font-weight: 650;
		margin: 0;
	}
	.area-meta {
		margin: 2px 0 0;
		color: var(--ink-3);
		font-size: var(--fs-cap);
		overflow-wrap: anywhere;
	}
	.inactive .area-name {
		color: var(--ink-2);
	}
	.area-actions {
		display: flex;
		align-items: center;
		gap: 12px;
		flex: none;
	}
	.import {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 20px;
	}
	.import fieldset {
		border: 0;
		padding: 0;
		margin: 0;
		min-width: 0;
	}
	.import-list {
		max-height: 320px;
		overflow-y: auto;
	}
	@media (max-width: 767px) {
		/* Card: name/meta + chevron on top (tappable), controls below. */
		.area-row {
			display: grid;
			grid-template-columns: auto minmax(0, 1fr) auto;
			grid-template-areas: 'chip main chev' 'act act act';
			row-gap: 10px;
		}
		.drag {
			display: none;
		}
		.area-chip {
			grid-area: chip;
			align-self: start;
		}
		.area-main {
			grid-area: main;
		}
		.area-chev {
			grid-area: chev;
		}
		.area-actions {
			grid-area: act;
			justify-content: space-between;
		}
		.import {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
