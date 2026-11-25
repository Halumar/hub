<script>
	/**
	 * Editor header: back, title + counts, inline "Saved", add actions and the
	 * area toolbar (active switch, control mode, visibility, grid). On phones
	 * the add actions move to the bottom bar of the page.
	 */
	import { base } from '$app/paths';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Menu from '$lib/ui/Menu.svelte';
	import Switch from '$lib/ui/Switch.svelte';

	let {
		area,
		entities = 0,
		widgets = 0,
		sectionsCount = 0,
		saved = false,
		globalMode = '',
		onToggleActive,
		onCopyId,
		onEditArea,
		onOpenSync,
		onAddSection,
		onAddWidget,
		onAddEntity,
	} = $props();

	let modeText = $derived(
		area.control_mode
			? t(`areas.modes.${area.control_mode}`)
			: globalMode
				? t('areas.modes.defaultWith', { mode: t(`areas.modes.${globalMode}`) })
				: t('areas.modes.default'),
	);
</script>

<header class="eh">
	<a href="{base}/areas" class="btn btn-icon" aria-label={t('editor.backToAreas')}><Icon name="back" /></a>
	<div class="grow min-w-0">
		<h1 class="t-page">{area.name}</h1>
		<p class="t-sub">
			{t('editor.subtitle', {
				entities: t('areas.entities', { count: entities }),
				widgets: t('areas.widgets', { count: widgets }),
				sections: t('areas.sections', { count: sectionsCount }),
			})}
		</p>
	</div>
	<span class="saved" class:show={saved} role="status" aria-live="polite">
		{#if saved}<Icon name="check" />{t('editor.saved')}{/if}
	</span>
	<div class="row eh-actions">
		<button type="button" class="btn" onclick={onEditArea} data-testid="area-properties"><Icon name="sliders" />{t('editor.properties')}</button>
		<button type="button" class="btn desk" onclick={onAddSection} aria-label={t('editor.addSectionLong')}><Icon name="plus" />{t('editor.addSection')}</button>
		<button type="button" class="btn desk" onclick={onAddWidget} aria-label={t('editor.addWidgetLong')}><Icon name="plus" />{t('editor.addWidget')}</button>
		<button type="button" class="btn btn-primary desk" onclick={onAddEntity} aria-label={t('editor.addEntityLong')}><Icon name="plus" />{t('editor.addEntity')}</button>
		<Menu
			iconOnly
			ariaLabel={t('editor.moreActions')}
			buttonClass="btn"
			items={[
				{ label: t('editor.syncFromHa'), icon: 'sync', onclick: onOpenSync, disabled: !area.ha_area_id },
				{ label: t('areas.copyId'), icon: 'link', onclick: () => onCopyId?.(area.id) },
			]}
		/>
	</div>
</header>

<div class="toolbar">
	<Switch checked={area.is_active} label={t('editor.activeLabel')} onchange={onToggleActive} testid="area-active" />
	<span class="badge">{t('areas.controlModeValue', { mode: modeText })}</span>
	<span class="badge">{area.tags?.length ? t('areas.visibleFor', { tags: area.tags.join(', ') }) : t('areas.visibleForAll')}</span>
	<span class="badge">{t('editor.grid', { count: area.grid_cols || 2 })}</span>
</div>

<style>
	.eh {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		margin-bottom: 12px;
	}
	.eh .grow {
		flex: 1 1 220px;
	}
	.saved {
		min-width: 1px;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		margin-bottom: 16px;
	}
	@media (max-width: 767px) {
		.desk {
			display: none;
		}
		.eh-actions :global(.btn:not(.btn-icon)) {
			font-size: var(--fs-sm);
		}
		.toolbar .badge {
			display: none;
		}
	}
</style>
