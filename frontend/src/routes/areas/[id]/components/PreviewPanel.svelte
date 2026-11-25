<script>
	/**
	 * "Preview iPhone": schematic phone with the visible tiles of each section
	 * (max. 2 columns, concept INDEX ios/45). Purely illustrative.
	 */
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';

	let { sections = [] } = $props();

	function visible(section) {
		return section.items.filter((i) => i._type === 'widget' || i.is_visible !== false);
	}
</script>

<aside class="prev" aria-labelledby="prev-title">
	<h2 id="prev-title" class="prev-title"><Icon name="iphone" />{t('editor.previewTitle')}</h2>
	<div class="mini" aria-hidden="true">
		{#each sections as s (s.id)}
			{#if sections.length > 1}<b>{s.title || t('editor.sectionUntitled')}</b>{/if}
			{#each visible(s) as item (item.id)}
				<i class:w={(s.columns ?? 2) >= 2 && (item.columns || 1) >= 2} class:tall={(item.rows || 1) >= 2} class:on={item._type === 'entity' && item.entity_id?.startsWith('light.')} class:widget={item._type === 'widget'}></i>
			{/each}
		{/each}
	</div>
	<p class="t-help mt-2">{t('editor.previewNote')}</p>
</aside>

<style>
	.prev {
		background: var(--surface);
		border: 1px solid var(--line);
		border-radius: var(--r-card);
		padding: 12px;
		position: sticky;
		top: 0;
	}
	.prev-title {
		display: flex;
		align-items: center;
		gap: 6px;
		margin: 0 0 8px;
		font-size: var(--fs-sm);
		font-weight: 650;
	}
	.mini {
		border-radius: 22px;
		background: var(--bg);
		border: 6px solid #0b0b0e;
		padding: 10px;
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 6px;
		max-height: 420px;
		overflow: hidden;
	}
	:global([data-theme='dark']) .mini {
		border-color: #3a3d45;
	}
	.mini b {
		grid-column: span 2;
		font-size: 11px;
		color: var(--ink-2);
		margin-top: 4px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mini i {
		height: 40px;
		border-radius: 10px;
		background: var(--surface);
		box-shadow: 0 1px 2px rgb(0 0 0 / 0.08);
		display: block;
	}
	.mini i.w {
		grid-column: span 2;
	}
	.mini i.tall {
		height: 86px;
		grid-row: span 2;
	}
	.mini i.on {
		background: var(--light-soft);
	}
	.mini i.widget {
		background: transparent;
		border: 1px dashed var(--line-2);
		box-shadow: none;
	}
</style>
