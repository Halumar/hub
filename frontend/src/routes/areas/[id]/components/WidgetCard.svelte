<script>
	/** One widget tile (data in, callbacks out), same layout rules as EntityCard. */
	import DOMPurify from 'dompurify';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Menu from '$lib/ui/Menu.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';

	let {
		widget,
		sectionColumns,
		index = 0,
		count = 1,
		deleting = false,
		onSetColumns,
		onSetRows,
		onEdit,
		onDelete,
		onMove,
	} = $props();

	let isSingleCol = $derived(sectionColumns === 1);
	let cols = $derived(isSingleCol ? 1 : widget.columns || 1);
	let rows = $derived(widget.rows || 1);
	let typeLabel = $derived(t(`editor.widgetTypes.${widget.widget_type}.label`));
	let cfg = $derived(widget.config || {});

	function inline(md) {
		const html = md
			.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
			.replace(/\*(.+?)\*/g, '<em>$1</em>')
			.replace(/`(.+?)`/g, '<code>$1</code>');
		return DOMPurify.sanitize(html, { ALLOWED_TAGS: ['strong', 'em', 'code'], ALLOWED_ATTR: [] });
	}

	let lines = $derived(
		(cfg.content || '')
			.split('\n')
			.map((l) => l.trim())
			.filter(Boolean)
			.slice(0, 4),
	);

	let menuItems = $derived([
		{ label: t('common.moveUp'), icon: 'arrowup', onclick: () => onMove?.(-1), disabled: index === 0 },
		{ label: t('common.moveDown'), icon: 'arrowdown', onclick: () => onMove?.(1), disabled: index >= count - 1 },
		{ separator: true },
		{ label: t('editor.editWidget'), icon: 'edit', onclick: () => onEdit?.() },
		...(isSingleCol ? [] : [{ label: cols === 2 ? t('editor.makeNarrow') : t('editor.makeWide'), icon: 'grid', onclick: () => onSetColumns?.(cols === 2 ? 1 : 2) }]),
		{ separator: true },
		{ label: t('editor.removeWidget'), icon: 'trash', danger: true, onclick: () => onDelete?.(), disabled: deleting },
	]);
</script>

<div class="tile" data-testid="widget-tile">
	<div class="tile-top">
		<span class="chipic"><Icon name="widget" /></span>
		<span class="tile-text">
			<b>{cfg.title || typeLabel}</b>
			<small>{t('editor.widget')} · {typeLabel}</small>
		</span>
		<Menu iconOnly ariaLabel={t('editor.actionsFor', { name: cfg.title || typeLabel })} items={menuItems} />
	</div>
	<div class="preview">
		{#if widget.widget_type === 'text' && lines.length}
			{#each lines as line, i (i)}
				{#if line.startsWith('#')}
					<p class="h" style:color={cfg.text_color || null}>{line.replace(/^#+\s*/, '')}</p>
				{:else if /^([-*+]|\d+\.)\s/.test(line)}
					<p>• {@html inline(line.replace(/^([-*+]|\d+\.)\s+/, ''))}</p>
				{:else}
					<p>{@html inline(line)}</p>
				{/if}
			{/each}
		{:else if widget.widget_type === 'clock'}
			<p>{cfg.show_date === false ? t('editor.clockOnly') : t('editor.clockDate')}</p>
		{:else if widget.widget_type !== 'text'}
			<p class="t-muted">{Object.keys(cfg).length ? JSON.stringify(cfg).slice(0, 60) : t('editor.noConfig')}</p>
		{/if}
	</div>
	<div class="tile-actions">
		<span class="t-help">{t('editor.width')}</span>
		<Segmented label={t('editor.widthOf', { name: typeLabel })} options={[1, 2].map((n) => ({ value: n, label: String(n) }))} value={cols} disabled={isSingleCol} onchange={(v) => onSetColumns?.(v)} />
		<span class="t-help">{t('editor.height')}</span>
		<Segmented label={t('editor.heightOf', { name: typeLabel })} options={[1, 2].map((n) => ({ value: n, label: String(n) }))} value={rows} onchange={(v) => onSetRows?.(v)} />
		<span class="spacer"></span>
		<button type="button" class="btn btn-sm" onclick={() => onEdit?.()}><Icon name="edit" />{t('common.edit')}</button>
		<button type="button" class="btn btn-sm btn-danger btn-icon" onclick={() => onDelete?.()} disabled={deleting} aria-label={t('editor.removeWidget')}><Icon name="trash" /></button>
	</div>
</div>

<style>
	.tile {
		height: 100%;
		box-sizing: border-box;
		border: 1px dashed var(--line-2);
		border-radius: var(--r-box);
		background: var(--bg);
		padding: 10px 12px;
		display: grid;
		gap: 8px;
		align-content: start;
		min-width: 0;
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
		font-size: var(--fs-cap);
	}
	.preview {
		font-size: var(--fs-sm);
		color: var(--ink-2);
		min-width: 0;
	}
	.preview p {
		margin: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.preview .h {
		font-weight: 700;
		color: var(--ink);
	}
	.preview :global(code) {
		font-family: var(--mono);
		background: var(--fill-2);
		border-radius: 4px;
		padding: 0 3px;
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
			border-bottom: 1px solid var(--line);
		}
		.tile-actions {
			display: none;
		}
	}
</style>
