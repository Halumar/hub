<script>
	/**
	 * The section grid of the area editor. The parent owns the `sections`
	 * array and all handlers; this component renders and wires svelte-dnd-action
	 * (pointer drag on desktop). Every reorder is also possible with buttons /
	 * menu items (keyboard, touch) – `dragDisabled` turns drag off on phones.
	 */
	import { flip } from 'svelte/animate';
	import { dndzone } from 'svelte-dnd-action';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Menu from '$lib/ui/Menu.svelte';
	import EntityCard from './EntityCard.svelte';
	import WidgetCard from './WidgetCard.svelte';

	let {
		sections,
		gridCols,
		flipDurationMs,
		dragDisabled = false,
		onSectionConsider,
		onSectionFinalize,
		onItemConsider,
		onItemFinalize,
		onMoveSection,
		onMoveItem,
		onRenameSection,
		onUpdateSectionColumns,
		onDeleteSection,
		onAddEntityToSection,
		onAddWidgetToSection,
		deletingSectionId = null,
		removingEntityId = null,
		onEntitySetColumns,
		onEntitySetRows,
		onEntityToggleVisibility,
		onEntitySetControlMode,
		onEntityRemove,
		onEntityRename,
		onEntityCustomize = null,
		selectedEntityId = null,
		iconPaths = null,
		deletingWidgetId = null,
		onWidgetSetColumns,
		onWidgetSetRows,
		onWidgetEdit,
		onWidgetDelete,
		realWidgetId,
	} = $props();

	const dropTargetStyle = { outline: '2px dashed var(--accent)', outlineOffset: '2px', borderRadius: '14px' };

	function title(section) {
		return section.title || t('editor.sectionUntitled');
	}

	function counts(section) {
		const e = section.items.filter((i) => i._type === 'entity').length;
		const w = section.items.filter((i) => i._type === 'widget').length;
		return t('editor.sectionItems', { entities: t('areas.entities', { count: e }), widgets: t('areas.widgets', { count: w }) });
	}
</script>

<div
	class="sgrid cols-{gridCols}"
	use:dndzone={{ items: sections, flipDurationMs, type: 'sections', dragDisabled, dropTargetStyle }}
	onconsider={onSectionConsider}
	onfinalize={onSectionFinalize}
>
	{#each sections as section, sIdx (section.id)}
		<section class="sect" class:span-2={(section.columns ?? 2) >= 2} animate:flip={{ duration: flipDurationMs }} aria-labelledby="sec-{section.id}" data-testid="section">
			<header class="sh">
				<span class="drag" aria-hidden="true"><Icon name="drag" /></span>
				<h2 id="sec-{section.id}" class="sh-title">{title(section)}</h2>
				<small class="sh-count">{counts(section)}</small>
				<span class="spacer"></span>
				<button type="button" class="btn btn-sm btn-icon" onclick={() => onMoveSection?.(sIdx, -1)} disabled={sIdx === 0} aria-label={t('common.moveUpName', { name: title(section) })}><Icon name="arrowup" /></button>
				<button type="button" class="btn btn-sm btn-icon" onclick={() => onMoveSection?.(sIdx, 1)} disabled={sIdx === sections.length - 1} aria-label={t('common.moveDownName', { name: title(section) })}><Icon name="arrowdown" /></button>
				<Menu
					ariaLabel={t('editor.sectionActions', { name: title(section) })}
					buttonClass="btn btn-sm sh-more"
					items={[
						{ label: t('editor.renameSection'), icon: 'edit', onclick: () => onRenameSection?.(section) },
						{ label: (section.columns ?? 2) === 1 ? t('editor.sectionFull') : t('editor.sectionHalf'), icon: 'grid', onclick: () => onUpdateSectionColumns?.(section, (section.columns ?? 2) === 1 ? 2 : 1) },
						{ separator: true },
						{ label: t('editor.addEntityHere'), icon: 'plus', onclick: () => onAddEntityToSection?.(section.id) },
						{ label: t('editor.addWidgetHere'), icon: 'widget', onclick: () => onAddWidgetToSection?.(section.id) },
						{ separator: true },
						{ label: t('editor.deleteSection'), icon: 'trash', danger: true, disabled: deletingSectionId === section.id, onclick: () => onDeleteSection?.(section) },
					]}
				/>
			</header>

			<div
				class="egrid cols-{section.columns ?? 2}"
				use:dndzone={{ items: section.items, flipDurationMs, type: 'items', dragDisabled, dropTargetStyle }}
				onconsider={(e) => onItemConsider?.(section.id, e)}
				onfinalize={(e) => onItemFinalize?.(section.id, e)}
			>
				{#each section.items as item, iIdx (item.id)}
					<div
						class="cell"
						class:span-2={(section.columns ?? 2) >= 2 && (item.columns || 1) >= 2}
						class:rows-2={(item.rows || 1) >= 2}
						animate:flip={{ duration: flipDurationMs }}
					>
						{#if item._type === 'entity'}
							<EntityCard
								entity={item}
								sectionColumns={section.columns ?? 2}
								index={iIdx}
								count={section.items.length}
								removing={removingEntityId === item.entity_id}
								onSetColumns={(n) => onEntitySetColumns?.(item, n)}
								onSetRows={(n) => onEntitySetRows?.(item, n)}
								onToggleVisibility={() => onEntityToggleVisibility?.(item)}
								onSetControlMode={(mode) => onEntitySetControlMode?.(item, mode)}
								onRemove={() => onEntityRemove?.(item)}
								onRename={() => onEntityRename?.(item)}
								onMove={(dir) => onMoveItem?.(section.id, iIdx, dir)}
								onCustomize={onEntityCustomize ? () => onEntityCustomize(item, section) : null}
								selected={selectedEntityId === item.entity_id}
								{iconPaths}
							/>
						{:else}
							<WidgetCard
								widget={item}
								sectionColumns={section.columns ?? 2}
								index={iIdx}
								count={section.items.length}
								deleting={deletingWidgetId === realWidgetId?.(item)}
								onSetColumns={(n) => onWidgetSetColumns?.(item, n)}
								onSetRows={(n) => onWidgetSetRows?.(item, n)}
								onEdit={() => onWidgetEdit?.(item)}
								onDelete={() => onWidgetDelete?.(item)}
								onMove={(dir) => onMoveItem?.(section.id, iIdx, dir)}
							/>
						{/if}
					</div>
				{/each}
				{#if section.items.length === 0}
					<p class="empty">{t('editor.emptySection')}</p>
				{/if}
			</div>
		</section>
	{/each}
</div>

<style>
	.sgrid {
		display: grid;
		gap: 14px;
		grid-template-columns: minmax(0, 1fr);
		align-items: start;
	}
	@media (min-width: 1024px) {
		.sgrid.cols-2 {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.sgrid.cols-3 {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}
		.sgrid.cols-4 {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
		.sect.span-2 {
			grid-column: span 2;
		}
	}
	.sect {
		background: var(--surface);
		border: 1px solid var(--line);
		border-radius: var(--r-card);
		min-width: 0;
	}
	.sh {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--line);
		flex-wrap: wrap;
	}
	.drag {
		color: var(--ink-3);
		cursor: grab;
		display: grid;
	}
	.sh-title {
		font-size: var(--fs-t3);
		font-weight: 650;
		margin: 0;
		overflow-wrap: anywhere;
	}
	.sh-count {
		color: var(--ink-3);
		font-size: var(--fs-cap);
	}
	.egrid {
		display: grid;
		gap: 10px;
		padding: 12px;
		grid-template-columns: minmax(0, 1fr);
		grid-auto-rows: minmax(0, auto);
		min-height: 48px;
	}
	.egrid.cols-2 {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}
	.cell {
		min-width: 0;
	}
	.cell.span-2 {
		grid-column: span 2;
	}
	.cell.rows-2 {
		grid-row: span 2;
	}
	.empty {
		grid-column: 1 / -1;
		margin: 0;
		padding: 12px;
		text-align: center;
		color: var(--ink-3);
		font-size: var(--fs-sm);
	}
	@media (max-width: 767px) {
		.drag,
		.sh-count {
			display: none;
		}
		.sh :global(.sh-more) {
			width: var(--h-ctl-sm);
			padding: 0;
			font-size: 0;
			gap: 0;
		}
		.egrid,
		.egrid.cols-2 {
			grid-template-columns: minmax(0, 1fr);
			gap: 0;
			padding: 0;
		}
		.cell.span-2,
		.cell.rows-2 {
			grid-column: auto;
			grid-row: auto;
		}
	}
</style>
