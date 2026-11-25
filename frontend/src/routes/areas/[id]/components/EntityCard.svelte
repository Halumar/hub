<script>
	/**
	 * One entity tile (data in, callbacks out). Desktop: width/height segments,
	 * hide and remove inline; every action is also in the "…" menu, which is
	 * the only control on phones (one row per entity, concept "Area editor mobile").
	 */
	import { t } from '$lib/i18n/index.svelte.js';
	import { domainIcon } from '$lib/data/domains.js';
	import { badgeList } from '$lib/data/tiles.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Menu from '$lib/ui/Menu.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';

	let {
		entity,
		sectionColumns,
		index = 0,
		count = 1,
		removing = false,
		onSetColumns,
		onSetRows,
		onToggleVisibility,
		onSetControlMode,
		onRemove,
		onRename,
		onMove,
		onCustomize,
		selected = false,
		iconPaths = null,
	} = $props();

	const MODES = [null, 'basic', 'advanced', 'full'];
	let name = $derived(entity.display_name || entity.state?.attributes?.friendly_name || entity.entity_id);
	let isSingleCol = $derived(sectionColumns === 1);
	let cols = $derived(isSingleCol ? 1 : entity.columns || 1);
	let rows = $derived(entity.rows || 1);
	let tile = $derived(entity.tile || {});
	// Custom MDI icon (curated path or hub output icon_svg); otherwise the stroke domain icon.
	let tilePath = $derived(
		tile.icon?.startsWith('mdi:') ? iconPaths?.get(tile.icon.slice(4)) || tile.icon_svg || null : null,
	);
	let badges = $derived(badgeList(tile));

	function badgeText(b) {
		if (b.key === 'tiles.badges.accent') return t(b.key, { color: t(`tiles.accents.${b.color}`) });
		if (b.key === 'tiles.badges.tap') {
			const what = b.tap.action === 'run' ? b.tap.target_entity_id : t(`tiles.status.tap.${b.tap.action}`);
			return t(b.key, { what });
		}
		if (b.key === 'tiles.badges.hidden') return t(b.key, { device: t(`tiles.badgeDevice.${b.device}`) });
		return t(b.key);
	}

	function modeLabel(m) {
		return m ? t(`areas.modes.${m}`) : t('areas.modes.default');
	}

	let menuItems = $derived([
		...(onCustomize ? [{ label: t('tiles.customize'), icon: 'sliders', onclick: () => onCustomize?.(), testid: 'item-customize' }, { separator: true }] : []),
		{ label: t('common.moveUp'), icon: 'arrowup', onclick: () => onMove?.(-1), disabled: index === 0, testid: 'item-move-up' },
		{ label: t('common.moveDown'), icon: 'arrowdown', onclick: () => onMove?.(1), disabled: index >= count - 1, testid: 'item-move-down' },
		{ separator: true },
		{ label: t('editor.rename'), icon: 'edit', onclick: () => onRename?.() },
		{ label: entity.is_visible ? t('editor.hide') : t('editor.show'), icon: entity.is_visible ? 'eyeoff' : 'eye', onclick: () => onToggleVisibility?.() },
		...(isSingleCol
			? []
			: [{ label: cols === 2 ? t('editor.makeNarrow') : t('editor.makeWide'), icon: 'grid', onclick: () => onSetColumns?.(cols === 2 ? 1 : 2) }]),
		{ separator: true },
		...MODES.map((m) => ({
			label: `${t('editor.controlMode')}: ${modeLabel(m)}`,
			icon: (entity.control_mode ?? null) === m ? 'check' : 'sparkle',
			onclick: () => onSetControlMode?.(m),
		})),
		{ separator: true },
		{ label: t('common.remove'), icon: 'trash', danger: true, onclick: () => onRemove?.(), disabled: removing },
	]);
</script>

<div class="tile rowlink" class:muted={!entity.is_visible} class:selected data-testid="entity-tile">
	<div class="tile-top">
		<span class="chipic">
			{#if tilePath}<svg viewBox="0 0 24 24" class="ic" aria-hidden="true"><path fill="currentColor" d={tilePath} /></svg>{:else}<Icon name={domainIcon(entity.entity_id)} />{/if}
		</span>
		<span class="tile-text">
			{#if onCustomize}
				<button
					type="button"
					class="rowlink-target tile-open"
					aria-haspopup="dialog"
					aria-controls="tile-panel"
					aria-expanded={selected ? 'true' : 'false'}
					aria-current={selected ? 'true' : undefined}
					aria-label={t('tiles.customizeName', { name })}
					data-tile-open={entity.entity_id}
					onclick={() => onCustomize?.()}
				><b>{name}</b></button>
			{:else}
				<b>{name}</b>
			{/if}
			<small class="t-mono">{entity.entity_id}</small>
		</span>
		{#if !entity.is_visible}<span class="badge"><Icon name="eyeoff" />{t('editor.hidden')}</span>{/if}
		{#if entity.control_mode}<span class="badge badge-info tile-mode">{modeLabel(entity.control_mode)}</span>{/if}
		<span class="rowlink-ctl"><Menu iconOnly buttonClass="btn btn-sm" ariaLabel={t('editor.actionsFor', { name })} items={menuItems} /></span>
	</div>
	{#if badges.length}
		<div class="tile-badges" data-testid="tile-badges">
			{#each badges as b, i (i)}<span class="badge" class:badge-accent={b.tone === 'acc'} class:badge-warn={b.tone === 'warn'} class:badge-err={b.tone === 'err'}>{badgeText(b)}</span>{/each}
		</div>
	{/if}
	<div class="tile-actions rowlink-ctl">
		<span class="t-help">{t('editor.width')}</span>
		<Segmented
			label={t('editor.widthOf', { name })}
			options={[1, 2].map((n) => ({ value: n, label: String(n), title: isSingleCol ? t('editor.widthLocked') : undefined }))}
			value={cols}
			disabled={isSingleCol}
			onchange={(v) => onSetColumns?.(v)}
		/>
		<span class="t-help">{t('editor.height')}</span>
		<Segmented label={t('editor.heightOf', { name })} options={[1, 2].map((n) => ({ value: n, label: String(n) }))} value={rows} onchange={(v) => onSetRows?.(v)} />
		<span class="spacer"></span>
		<button type="button" class="btn btn-sm" onclick={() => onToggleVisibility?.()} aria-label={entity.is_visible ? t('editor.hideName', { name }) : t('editor.showName', { name })}>
			<Icon name={entity.is_visible ? 'eyeoff' : 'eye'} />{entity.is_visible ? t('editor.hide') : t('editor.show')}
		</button>
		<button type="button" class="btn btn-sm btn-danger btn-icon" onclick={() => onRemove?.()} disabled={removing} aria-label={t('editor.removeName', { name })}>
			<Icon name="trash" />
		</button>
	</div>
</div>

<style>
	.tile {
		height: 100%;
		box-sizing: border-box;
		border: 1px solid var(--line);
		border-radius: var(--r-box);
		background: var(--bg);
		padding: 10px 12px;
		display: grid;
		gap: 8px;
		align-content: start;
		min-width: 0;
	}
	.tile.selected {
		border-color: var(--accent);
		box-shadow: 0 0 0 1px var(--accent);
	}
	.tile-open {
		display: block;
		max-width: 100%;
		padding: 0;
		border: 0;
		background: none;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.tile-badges {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.tile.muted .tile-text b,
	.tile.muted .chipic {
		opacity: 0.6;
	}
	.tile-top {
		display: flex;
		align-items: center;
		gap: 10px;
		min-width: 0;
	}
	.tile-text {
		flex: 1;
		min-width: 0;
	}
	.tile-text b,
	.tile-text small {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.tile-text b {
		font-weight: 650;
	}
	.tile-text small {
		color: var(--ink-3);
	}
	.tile-actions {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	@media (max-width: 767px) {
		.tile {
			border: 0;
			border-radius: 0;
			background: none;
			padding: 10px 12px;
			border-bottom: 1px solid var(--line);
		}
		.tile-actions,
		.tile-mode,
		.tile-badges {
			display: none;
		}
	}
</style>
