<script>
	import { onMount, onDestroy, untrack } from 'svelte';
	import { base } from '$app/paths';
	import { page } from '$app/stores';
	import { api } from '$lib/api.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import { copyText, debounce } from '$lib/utils.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import PreviewPanel from './components/PreviewPanel.svelte';
	import TilePanel from './components/TilePanel.svelte';
	import RenameModal from './components/RenameModal.svelte';

	import AreaHeader from './components/AreaHeader.svelte';
	import SectionGrid from './components/SectionGrid.svelte';
	import AddEntityModal from './components/AddEntityModal.svelte';
	import AddSectionModal from './components/AddSectionModal.svelte';
	import AddWidgetModal from './components/AddWidgetModal.svelte';
	import EditWidgetModal from './components/EditWidgetModal.svelte';
	import EditAreaModal from '$lib/components/EditAreaModal.svelte';
	import SyncFromHaModal from './components/SyncFromHaModal.svelte';
	import RoomClimateCard from './components/RoomClimateCard.svelte';
	import SceneHints from '$lib/components/profiles/SceneHints.svelte';
	import { areaInRule } from '$lib/profiles/scene-hints.js';

	let area = $state(null);
	let areaVersion = $state(0);
	let haEntities = $state([]);
	let haEntitiesLoading = $state(false);
	let haEntitiesError = $state('');
	let loading = $state(true);
	let loadingAreaId = null; // Track which area ID is being loaded to prevent race conditions
	let loadError = $state('');
	const flipDurationMs = 200;
	let globalMode = $state('');

	// Inline "Saved" after own changes (concept: no full-page spinner).
	let saved = $state(false);
	let savedTimer;
	function markSaved() {
		saved = true;
		clearTimeout(savedTimer);
		savedTimer = setTimeout(() => (saved = false), 2500);
	}

	// Phones: no drag (touch scrolling); reorder with buttons/menus instead.
	let isPhone = $state(false);

	// Rename dialog (sections and entities)
	let renameOpen = $state(false);
	let renameTitle = $state('');
	let renameValue = $state('');
	let renameSave = $state(() => {});

	// Modal visibility / targets
	let showAddEntity = $state(false);
	let showEditModal = $state(false);
	let showAddSection = $state(false);
	let showAddWidget = $state(false);
	let showSyncModal = $state(false);
	let addingEntityToSectionId = $state(null);
	let addWidgetDefaultSection = $state(null);
	let editWidgetTarget = $state(null);

	// ── Confirm dialog (shared destructive-action dialog) ──────────────
	let confirmOpen = $state(false);
	let confirmTitle = $state('');
	let confirmMessage = $state('');
	let confirmLabel = $state('');
	let confirmAction = $state(() => {});

	function askConfirm({ title = '', message, confirmLabel: label = '', action }) {
		confirmTitle = title;
		confirmMessage = message;
		confirmLabel = label;
		confirmAction = action;
		confirmOpen = true;
	}

	// ── Tile panel ──────────────────────────────────────────────
	let tileCaps = $state(null);
	let tileCapsError = $state('');
	let tileEntityId = $state(null);
	let tileDirty = $state(false);
	let tileIconPaths = $derived(new Map((tileCaps?.icons?.icons || []).map((i) => [i.mdi, i.path])));

	async function ensureTileCaps() {
		if (tileCaps) return true;
		try {
			tileCaps = await api.getTileCapabilities();
			tileCapsError = '';
			return true;
		} catch (e) {
			tileCapsError = e.message;
			addToast(t('tiles.loadFailed', { message: e.message }), 'error');
			return false;
		}
	}

	/** Run `next` now, or after "discard changes?" when the panel has edits. */
	function withDiscard(next) {
		if (!tileDirty) return next();
		const name = tileEntity?.display_name || tileEntityId;
		askConfirm({
			title: t('tiles.discardTitle'),
			message: t('tiles.discard', { name }),
			confirmLabel: t('tiles.discardConfirm'),
			action: () => {
				tileDirty = false;
				next();
			},
		});
	}

	async function openTile(entity) {
		if (tileEntityId === entity.entity_id) return;
		if (!(await ensureTileCaps())) return;
		withDiscard(() => (tileEntityId = entity.entity_id));
	}

	function closeTile(force = false) {
		const id = tileEntityId;
		const done = () => {
			tileEntityId = null;
			tileDirty = false;
			// Focus back to the tile that opened the panel.
			requestAnimationFrame(() => document.querySelector(`[data-tile-open="${CSS.escape(id || '')}"]`)?.focus());
		};
		if (force) done();
		else withDiscard(done);
	}

	let tileEntity = $derived.by(() => {
		if (!tileEntityId) return null;
		for (const s of dndSections) {
			const e = s.items.find((i) => i._type === 'entity' && i.entity_id === tileEntityId);
			if (e) return { ...e, section_id: s.id, _sectionColumns: s.columns ?? 2 };
		}
		return null;
	});
	let tilePreviewEntities = $derived(
		dndSections.flatMap((s) => s.items.filter((i) => i._type === 'entity' && i.is_visible !== 0 && i.is_visible !== false).map((i) => ({ ...i, section_id: s.id }))),
	);
	// The placement was removed elsewhere: close without asking.
	$effect(() => {
		if (tileEntityId && area && !tileEntity && !dndDragging) {
			tileEntityId = null;
			tileDirty = false;
		}
	});

	// WS broadcast unsubscribers
	let unsubs = [];

	$effect(() => {
		const id = $page.params.id;
		if (id) untrack(() => loadArea(id));
	});

	// Live events reload quietly (no spinner, scroll position kept); a burst of
	// events (e.g. bulk sync) collapses into one reload.
	const refresh = debounce(() => { if (area) loadArea(area.id); }, 200);

	onMount(() => {
		const mq = window.matchMedia('(max-width: 767px)');
		const onMq = () => (isPhone = mq.matches);
		onMq();
		mq.addEventListener('change', onMq);
		unsubs.push(() => mq.removeEventListener('change', onMq));
		api.getDefaultControlMode()
			.then((r) => (globalMode = r?.control_mode || ''))
			.catch(() => {});
		const forThisArea = (data) => { if (area && data?.area_id === area.id) refresh(); };
		unsubs.push(
			...['area_updated', 'entity_added', 'entity_updated', 'entity_removed', 'layout_updated',
				'widget_created', 'widget_updated', 'widget_deleted',
				'section_created', 'section_updated', 'section_deleted',
			].map((type) => onBroadcast(type, forThisArea)),
			onBroadcast('resync', () => refresh()),
			onBroadcast('area_deleted', (data) => {
				if (area && data.area_id === area.id) {
					addToast(t('editor.deletedElsewhere'), 'warning');
					area = null;
				}
			}),
		);
	});

	onDestroy(() => {
		refresh.cancel();
		refreshHints.cancel();
		clearTimeout(savedTimer);
		unsubs.forEach(fn => fn());
	});

	/**
	 * Load the area. The full-page spinner only shows when there is nothing to
	 * show yet (first load / navigation to another area); every later reload
	 * swaps the data in place so scroll position and context stay.
	 */
	async function loadArea(id) {
		loadingAreaId = id;
		if (!area || area.id !== id) loading = true;
		try {
			const data = await api.getArea(id);
			if (loadingAreaId === id) {
				area = data;
				areaVersion += 1;
				loadError = '';
			}
		} catch (e) {
			if (loadingAreaId === id) {
				if (area && area.id === id) addToast(t('editor.refreshFailed', { message: e.message }), 'error');
				else {
					area = null;
					loadError = e.status === 404 ? '' : e.message;
				}
			}
		} finally {
			if (loadingAreaId === id) {
				loading = false;
			}
		}
	}

	function reload() {
		if (area) return loadArea(area.id);
	}

	// ── Scene hints: scenes and scripts placed here whose targets a profile with this area may not switch ──
	let sceneHints = $state([]);
	let areaNames = $state(new Map());
	let hintsFor = '';

	async function loadSceneHints(a) {
		const id = a.id;
		hintsFor = id;
		try {
			const [list, areas] = await Promise.all([api.listProfiles(), api.getAreas().catch(() => [])]);
			areaNames = new Map((Array.isArray(areas) ? areas : []).map((x) => [x.id, x.name]));
			const tags = a.tags || [];
			const relevant = (list?.profiles || [])
				.filter((p) => areaInRule(p.rules?.areas, id, () => tags))
				.sort((x, y) => (y.device_count > 0) - (x.device_count > 0));
			const details = await Promise.allSettled(relevant.map((p) => api.getProfile(p.id)));
			if (hintsFor !== id) return;
			sceneHints = details
				.filter((d) => d.status === 'fulfilled')
				.map((d) => ({ profile: d.value, hints: (d.value.scene_hints || []).filter((h) => h.area_ids?.includes(id)) }))
				.filter((x) => x.hints.length);
		} catch {
			if (hintsFor === id) sceneHints = [];
		}
	}

	const refreshHints = debounce(() => { if (area) loadSceneHints(area); }, 400);
	$effect(() => {
		if (area?.id) untrack(() => refreshHints());
	});

	// Outer section grid columns — read from area.grid_cols (2-4, default 2).
	// Sections span 1 or 2 based on their own columns setting, clamped to gridCols.
	let gridCols = $derived(Math.max(2, Math.min(4, area?.grid_cols ?? 2)));

	// DnD state: mutable arrays that svelte-dnd-action can manipulate.
	// We sync from `area` on load and persist to backend on finalize.
	let dndSections = $state([]);     // [{id, title, column_span, columns, sort_order, items: [...]}]
	let dndDragging = $state(false);  // True while any drag is in progress

	// Build DnD section model from area data
	function buildDndSections(areaData) {
		if (!areaData?.sections) return [];
		return areaData.sections.toSorted((a, b) => a.sort_order - b.sort_order).map(s => ({
			id: s.id,
			title: s.title,
			icon: s.icon,
			column_span: s.column_span || 1,
			columns: s.columns ?? 2,
			sort_order: s.sort_order,
			is_collapsed: s.is_collapsed,
			items: [
				...(s.entities || []).map(e => ({ ...e, id: 'e:' + e.entity_id, _type: 'entity' })),
				...(s.widgets || []).map(w => ({ ...w, id: 'w:' + w.id, _type: 'widget' })),
			].sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0)),
		}));
	}

	// Sync DnD state whenever area data changes (but not during a drag)
	$effect(() => {
		if (area && !dndDragging) {
			dndSections = buildDndSections(area);
		}
	});

	// Read-only helpers derived from dndSections
	let sections = $derived(dndSections);

	// Get all entity IDs across all sections (for add entity filtering)
	let allEntityIds = $derived.by(() => {
		const ids = new Set();
		for (const s of dndSections) {
			for (const item of s.items) {
				if (item._type === 'entity') ids.add(item.entity_id);
			}
		}
		return ids;
	});

	// Total entity count
	let totalEntities = $derived.by(() => {
		let count = 0;
		for (const s of dndSections) {
			count += s.items.filter(i => i._type === 'entity').length;
		}
		return count;
	});

	let totalWidgets = $derived.by(() => {
		let count = 0;
		for (const s of dndSections) {
			count += s.items.filter(i => i._type === 'widget').length;
		}
		return count;
	});

	// ── DnD handlers ──────────────────────────────────────────────────

	function handleSectionConsider(e) {
		dndDragging = true;
		dndSections = e.detail.items;
	}

	async function persistSectionOrder() {
		try {
			await api.reorderSections(area.id, dndSections.map((s) => s.id));
			markSaved();
		} catch (err) {
			addToast(t('editor.orderFailed', { message: err.message }), 'error');
			await loadArea(area.id);
		}
	}

	async function persistLayout() {
		const layoutItems = [];
		for (const s of dndSections) {
			s.items.forEach((item, i) => {
				if (item._type === 'entity') layoutItems.push({ entity_id: item.entity_id, section_id: s.id, sort_order: i });
				else layoutItems.push({ widget_id: realWidgetId(item), section_id: s.id, sort_order: i });
			});
		}
		try {
			await api.updateLayout(area.id, layoutItems);
			markSaved();
		} catch (err) {
			addToast(t('editor.orderFailed', { message: err.message }), 'error');
			await loadArea(area.id);
		}
	}

	async function handleSectionFinalize(e) {
		dndSections = e.detail.items;
		dndDragging = false;
		await persistSectionOrder();
	}

	function handleItemConsider(sectionId, e) {
		dndDragging = true;
		dndSections = dndSections.map((s) => (s.id === sectionId ? { ...s, items: e.detail.items } : s));
	}

	async function handleItemFinalize(sectionId, e) {
		dndSections = dndSections.map((s) => (s.id === sectionId ? { ...s, items: e.detail.items } : s));
		dndDragging = false;
		await persistLayout();
	}

	/** Buttons/menus (keyboard, touch): move a section by one. */
	async function moveSection(idx, dir) {
		const to = idx + dir;
		if (to < 0 || to >= dndSections.length) return;
		const next = [...dndSections];
		[next[idx], next[to]] = [next[to], next[idx]];
		dndSections = next;
		await persistSectionOrder();
	}

	/** Buttons/menus: move an entity/widget by one inside its section. */
	async function moveItem(sectionId, idx, dir) {
		const sec = dndSections.find((s) => s.id === sectionId);
		const to = idx + dir;
		if (!sec || to < 0 || to >= sec.items.length) return;
		const items = [...sec.items];
		[items[idx], items[to]] = [items[to], items[idx]];
		dndSections = dndSections.map((s) => (s.id === sectionId ? { ...s, items } : s));
		await persistLayout();
	}

	// ── Add Entity ─────────────────────────────────────────────────────

	async function openAddEntity(sectionId = null) {
		addingEntityToSectionId = sectionId || sections[0]?.id || null;
		showAddEntity = true;
		haEntitiesError = '';
		if (haEntities.length === 0) haEntitiesLoading = true;
		try {
			haEntities = await api.getHAEntities();
		} catch (e) {
			haEntitiesError = t('editor.entitiesFailed', { message: e.message });
		} finally {
			haEntitiesLoading = false;
		}
	}

	async function addEntity(entityId, friendlyName) {
		if (!addingEntityToSectionId) {
			addToast(t('editor.noSectionSelected'), 'error');
			return;
		}
		try {
			await api.addEntity(area.id, {
				entity_id: entityId,
				section_id: addingEntityToSectionId,
				display_name: friendlyName,
			});
			addToast(t('editor.entityAdded', { name: friendlyName || entityId }), 'success');
			markSaved();
			await loadArea(area.id);
		} catch (e) {
			addToast(t('editor.entityAddFailed', { message: e.message }), 'error');
		}
	}

	// ── Entity actions ────────────────────────────────────────────────

	let removingEntityId = $state(null);

	/** PATCH one entity, then reload quietly. */
	async function patchEntity(entity, data) {
		try {
			await api.updateEntity(area.id, entity.entity_id, data);
			markSaved();
			await loadArea(area.id);
			return true;
		} catch (e) {
			addToast(t('editor.changeFailed', { message: e.message }), 'error');
			return false;
		}
	}

	function removeEntity(entity) {
		const name = entity.display_name || entity.entity_id;
		askConfirm({
			title: t('editor.removeEntityTitle'),
			message: t('editor.removeEntityMessage', { name }),
			confirmLabel: t('common.remove'),
			action: async () => {
				removingEntityId = entity.entity_id;
				try {
					await api.removeEntity(area.id, entity.entity_id);
					addToast(t('editor.entityRemoved', { name }), 'success');
					markSaved();
					await loadArea(area.id);
				} catch (e) {
					addToast(t('editor.changeFailed', { message: e.message }), 'error');
				} finally {
					removingEntityId = null;
				}
			},
		});
	}

	const setEntityColumns = (entity, n) => patchEntity(entity, { columns: n });
	const setEntityRows = (entity, n) => patchEntity(entity, { rows: n });
	const toggleEntityVisibility = (entity) => patchEntity(entity, { is_visible: !entity.is_visible });
	const setEntityControlMode = (entity, mode) => patchEntity(entity, { control_mode: mode });

	function openRename(title, value, save) {
		renameTitle = title;
		renameValue = value;
		renameSave = save;
		renameOpen = true;
	}

	function renameEntity(entity) {
		const name = entity.display_name || entity.entity_id;
		openRename(t('editor.renameTitle', { name }), name, async (v) => {
			if (await patchEntity(entity, { display_name: v })) addToast(t('editor.renamed', { name: v }), 'success');
		});
	}

	async function toggleAreaActive(next) {
		try {
			await api.patchArea(area.id, { is_active: next });
			markSaved();
			await loadArea(area.id);
			return true;
		} catch (e) {
			addToast(t('areas.toggleFailed', { message: e.message }), 'error');
			return false;
		}
	}

	// ── Section management ────────────────────────────────────────────

	let creatingSection = $state(false);

	async function createSection(title) {
		if (!area) return;
		creatingSection = true;
		const name = (title || '').trim() || t('editor.sectionDefaultTitle');
		try {
			await api.createSection(area.id, { title: name, sort_order: sections.length });
			showAddSection = false;
			addToast(t('editor.sectionCreated'), 'success');
			markSaved();
			await loadArea(area.id);
		} catch (e) {
			addToast(t('editor.changeFailed', { message: e.message }), 'error');
		} finally {
			creatingSection = false;
		}
	}

	let deletingSectionId = $state(null);

	function deleteSection(section) {
		const itemCount = section.items?.length || 0;
		const name = section.title || t('editor.sectionUntitled');
		askConfirm({
			title: t('editor.deleteSectionTitle'),
			message: itemCount > 0 ? t('editor.deleteSectionItems', { name, count: itemCount }) : t('editor.deleteSectionEmpty', { name }),
			confirmLabel: t('common.delete'),
			action: async () => {
				deletingSectionId = section.id;
				try {
					await api.deleteSection(area.id, section.id);
					addToast(t('editor.sectionDeleted'), 'success');
					markSaved();
					await loadArea(area.id);
				} catch (e) {
					addToast(t('editor.changeFailed', { message: e.message }), 'error');
				} finally {
					deletingSectionId = null;
				}
			},
		});
	}

	async function patchSection(section, data) {
		try {
			await api.updateSection(area.id, section.id, data);
			markSaved();
			await loadArea(area.id);
			return true;
		} catch (e) {
			addToast(t('editor.changeFailed', { message: e.message }), 'error');
			return false;
		}
	}

	function renameSection(section) {
		openRename(t('editor.renameSection'), section.title || '', (v) => patchSection(section, { title: v }));
	}

	const updateSectionColumns = (section, cols) => patchSection(section, { columns: cols, column_span: cols });

	// ── Widget management ─────────────────────────────────────────────

	let creatingWidget = $state(false);
	let savingWidget = $state(false);
	let deletingWidgetId = $state(null);

	function getWidgetTypeLabel(type) {
		return t(`editor.widgetTypes.${type}.label`);
	}

	/** Strip 'w:' prefix from DnD widget IDs to get the real DB id. */
	function realWidgetId(widget) {
		return typeof widget.id === 'string' && widget.id.startsWith('w:') ? widget.id.slice(2) : widget.id;
	}

	function openAddWidget(sectionId = null) {
		addWidgetDefaultSection = sectionId || sections[0]?.id || null;
		showAddWidget = true;
	}

	async function createWidget(payload) {
		if (!area || !payload?.section_id) return;
		creatingWidget = true;
		try {
			await api.createWidget(area.id, {
				section_id: payload.section_id,
				widget_type: payload.widget_type,
				config: payload.config,
			});
			showAddWidget = false;
			addToast(t('editor.widgetAdded'), 'success');
			markSaved();
			await loadArea(area.id);
		} catch (e) {
			addToast(t('editor.widgetFailed', { message: e.message }), 'error');
		} finally {
			creatingWidget = false;
		}
	}

	function deleteWidget(widget) {
		askConfirm({
			title: t('editor.removeWidgetTitle'),
			message: t('editor.removeWidgetMessage', { type: getWidgetTypeLabel(widget.widget_type) }),
			confirmLabel: t('common.remove'),
			action: async () => {
				deletingWidgetId = realWidgetId(widget);
				try {
					await api.deleteWidget(area.id, realWidgetId(widget));
					addToast(t('editor.widgetRemoved'), 'success');
					markSaved();
					await loadArea(area.id);
				} catch (e) {
					addToast(t('editor.widgetFailed', { message: e.message }), 'error');
				} finally {
					deletingWidgetId = null;
				}
			},
		});
	}

	async function patchWidget(widget, data) {
		try {
			await api.updateWidget(area.id, realWidgetId(widget), data);
			markSaved();
			await loadArea(area.id);
		} catch (e) {
			addToast(t('editor.widgetFailed', { message: e.message }), 'error');
		}
	}
	const setWidgetColumns = (widget, n) => patchWidget(widget, { columns: n });
	const setWidgetRows = (widget, n) => patchWidget(widget, { rows: n });

	function startEditWidget(widget) {
		editWidgetTarget = widget;
	}

	async function saveEditWidget(config) {
		const target = editWidgetTarget;
		if (!target) return;
		savingWidget = true;
		try {
			await api.updateWidget(area.id, realWidgetId(target), { config });
			editWidgetTarget = null;
			addToast(t('editor.widgetUpdated'), 'success');
			markSaved();
			await loadArea(area.id);
		} catch (e) {
			addToast(t('editor.widgetFailed', { message: e.message }), 'error');
		} finally {
			savingWidget = false;
		}
	}

	// ── Utilities ─────────────────────────────────────────────────────

	async function copyId(id) {
		const ok = await copyText(id);
		addToast(ok ? t('areas.idCopied') : t('common.copyFailed'), ok ? 'success' : 'error');
	}
</script>

<div class="page editor" class:has-bar={!!area}>
	{#if loading}
		<LoadingState />
	{:else if area}
		<AreaHeader
			{area}
			entities={totalEntities}
			widgets={totalWidgets}
			sectionsCount={sections.length}
			{saved}
			{globalMode}
			onToggleActive={toggleAreaActive}
			onCopyId={copyId}
			onEditArea={() => (showEditModal = true)}
			onOpenSync={() => (showSyncModal = true)}
			onAddSection={() => (showAddSection = true)}
			onAddWidget={() => openAddWidget()}
			onAddEntity={() => openAddEntity()}
		/>

		{#if sceneHints.length}
			<div class="hints" data-testid="area-scene-hints">
				{#each sceneHints as g (g.profile.id)}
					<SceneHints
						hints={g.hints}
						title={t('profiles.sceneHints.profileTitle', { name: g.profile.name })}
						areaName={(id) => areaNames.get(id) || id}
						addHref={() => `${base}/profiles/${g.profile.id}#r-areas`}
						currentAreaId={area.id}
						testid="area-scene-hints-{g.profile.id}"
					/>
				{/each}
			</div>
		{/if}

		<div class="ed" class:tile-open={!!tileEntity}>
			<div class="min-w-0">
				{#if !sections.length}
					<div class="card">
						<EmptyState icon="section" title={t('editor.noSections')} text={t('editor.noSectionsText')}>
							<button type="button" class="btn btn-primary" onclick={() => (showAddSection = true)}><Icon name="plus" />{t('editor.createSection')}</button>
						</EmptyState>
					</div>
				{:else}
					<SectionGrid
						sections={dndSections}
						{gridCols}
						{flipDurationMs}
						dragDisabled={isPhone}
						onSectionConsider={handleSectionConsider}
						onSectionFinalize={handleSectionFinalize}
						onItemConsider={handleItemConsider}
						onItemFinalize={handleItemFinalize}
						onMoveSection={moveSection}
						onMoveItem={moveItem}
						onRenameSection={renameSection}
						onUpdateSectionColumns={updateSectionColumns}
						onDeleteSection={deleteSection}
						onAddEntityToSection={openAddEntity}
						onAddWidgetToSection={openAddWidget}
						{deletingSectionId}
						{removingEntityId}
						onEntitySetColumns={setEntityColumns}
						onEntitySetRows={setEntityRows}
						onEntityToggleVisibility={toggleEntityVisibility}
						onEntitySetControlMode={setEntityControlMode}
						onEntityRemove={removeEntity}
						onEntityRename={renameEntity}
						onEntityCustomize={openTile}
						selectedEntityId={tileEntityId}
						iconPaths={tileIconPaths}
						{deletingWidgetId}
						onWidgetSetColumns={setWidgetColumns}
						onWidgetSetRows={setWidgetRows}
						onWidgetEdit={startEditWidget}
						onWidgetDelete={deleteWidget}
						{realWidgetId}
					/>
				{/if}
			</div>
			{#if tileEntity && tileCaps}
				<div class="ed-tile">
					<TilePanel
						{area}
						entity={tileEntity}
						sectionColumns={tileEntity._sectionColumns}
						previewEntities={tilePreviewEntities}
						caps={tileCaps}
						paths={tileIconPaths}
						bind:dirty={tileDirty}
						onclose={closeTile}
						onsaved={() => { markSaved(); return loadArea(area.id); }}
						onask={askConfirm}
					/>
				</div>
			{:else}
				<div class="ed-side">
					<RoomClimateCard areaId={area.id} version={areaVersion} />
					<PreviewPanel sections={dndSections} />
				</div>
			{/if}
		</div>

		<!-- Phones: add actions within thumb reach (concept "Area editor mobile") -->
		<div class="abar" role="group" aria-label={t('editor.mobileActions')}>
			<button type="button" class="btn" onclick={() => (showAddSection = true)} aria-label={t('editor.addSectionLong')}><Icon name="plus" />{t('editor.addSection')}</button>
			<button type="button" class="btn" onclick={() => openAddWidget()} aria-label={t('editor.addWidgetLong')}><Icon name="plus" />{t('editor.addWidget')}</button>
			<button type="button" class="btn btn-primary" onclick={() => openAddEntity()} aria-label={t('editor.addEntityLong')}><Icon name="plus" />{t('editor.addEntity')}</button>
		</div>
	{:else}
		<div class="card">
			{#if loadError}
				<ErrorState message={t('editor.loadFailed', { message: loadError })} onretry={() => loadArea($page.params.id)} />
			{:else}
				<EmptyState icon="grid" title={t('editor.notFound')}>
					<a class="btn" href="{base}/areas"><Icon name="back" />{t('editor.backToAreas')}</a>
				</EmptyState>
			{/if}
		</div>
	{/if}
</div>

<AddSectionModal bind:open={showAddSection} creating={creatingSection} oncreate={createSection} />
<AddWidgetModal bind:open={showAddWidget} {sections} defaultSectionId={addWidgetDefaultSection} creating={creatingWidget} oncreate={createWidget} />
<EditWidgetModal widget={editWidgetTarget} saving={savingWidget} onsave={saveEditWidget} onclose={() => (editWidgetTarget = null)} />
<AddEntityModal
	bind:open={showAddEntity}
	{sections}
	bind:sectionId={addingEntityToSectionId}
	entities={haEntities}
	loading={haEntitiesLoading}
	error={haEntitiesError}
	onretry={() => openAddEntity(addingEntityToSectionId)}
	{allEntityIds}
	onadd={addEntity}
/>
<RenameModal bind:open={renameOpen} title={renameTitle} label={t('editor.displayName')} value={renameValue} onsave={renameSave} />

{#if area}
	<EditAreaModal bind:open={showEditModal} {area} onSaved={reload} />
	<SyncFromHaModal bind:open={showSyncModal} {area} onReload={reload} />
{/if}

<ConfirmDialog bind:open={confirmOpen} title={confirmTitle} message={confirmMessage} {confirmLabel} danger onconfirm={confirmAction} />

<style>
	.hints {
		display: grid;
		gap: 10px;
		margin-bottom: 14px;
	}
	.ed {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 250px;
		gap: 18px;
		align-items: start;
	}
	.ed-side {
		position: sticky;
		top: 0;
		display: grid;
		gap: 14px;
	}
	.ed.tile-open {
		grid-template-columns: minmax(0, 1fr) 420px;
	}
	.ed-tile {
		position: sticky;
		top: 0;
		min-width: 0;
	}
	.abar {
		display: none;
	}
	@media (max-width: 1199px) {
		.ed {
			grid-template-columns: minmax(0, 1fr);
		}
		/* Narrow: the room climate below the sections, without the schematic preview. */
		.ed-side {
			position: static;
		}
		.ed-side :global(.prev) {
			display: none;
		}
	}
	@media (min-width: 1024px) and (max-width: 1199px) {
		.ed.tile-open {
			grid-template-columns: minmax(0, 1fr) 380px;
		}
	}
	@media (max-width: 1023px) {
		.ed.tile-open {
			grid-template-columns: minmax(0, 1fr);
		}
		/* The panel is a fixed full-screen sheet; no sticky stacking context here. */
		.ed-tile {
			position: static;
		}
	}
	@media (max-width: 767px) {
		.abar {
			display: flex;
			gap: 8px;
			position: fixed;
			left: 12px;
			right: 12px;
			bottom: calc(12px + env(safe-area-inset-bottom));
			z-index: 30;
			padding: 8px;
			border-radius: 20px;
			background: var(--surface);
			border: 1px solid var(--line);
			box-shadow: var(--shadow-pop);
		}
		.abar .btn {
			flex: 1;
			padding: 0 8px;
		}
	}
</style>
