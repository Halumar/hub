<script>
	/**
	 * Icon chooser of the tile panel: curated MDI↔SF set with DE/EN
	 * search words first, then "search all MDI icons" (lazy catalog) and the
	 * expert field for an SF Symbol name. Grids are radio groups (arrow keys).
	 *
	 * Props: value ('mdi:…' | 'sf:…' | null), icons (curated, with path),
	 * categories, locale, onchange(value|null, path|null), id (heading id)
	 */
	import { loadMdiIcons } from '$lib/data/mdi-lazy.js';
	import { validSf } from '$lib/data/tiles.js';
	import { t } from '$lib/i18n/index.svelte.js';

	let { value = null, icons = [], categories = [], locale = 'en', onchange = () => {}, labelledby = undefined } = $props();

	let query = $state('');
	let showAll = $state(false);
	let mdiList = $state(null);
	let mdiError = $state(false);
	let sfText = $state('');
	let root;

	$effect(() => {
		sfText = value?.startsWith('sf:') ? value.slice(3) : '';
	});

	const norm = (s) => s.toLocaleLowerCase().normalize('NFD').replace(/\p{Diacritic}/gu, '');

	let curated = $derived.by(() => {
		const q = norm(query.trim());
		const list = !q
			? icons
			: icons.filter((i) => i.mdi.includes(q) || [...(i.keywords?.[locale] || []), ...(i.keywords?.en || [])].some((k) => norm(k).includes(q)));
		return list;
	});
	let groups = $derived(
		categories
			.map((c) => ({ ...c, items: curated.filter((i) => i.category === c.id) }))
			.filter((g) => g.items.length),
	);
	let current = $derived(value?.startsWith('mdi:') ? value.slice(4) : null);
	let allResults = $derived.by(() => {
		if (!showAll || !mdiList) return [];
		const q = query.trim().toLowerCase().replace(/\s+/g, '-');
		if (!q) return [];
		const out = [];
		for (const [name, path] of mdiList) {
			if (name.includes(q)) {
				out.push({ name, path });
				if (out.length >= 60) break;
			}
		}
		return out;
	});

	function label(icon) {
		return icon.keywords?.[locale]?.[0] || icon.keywords?.en?.[0] || icon.mdi;
	}

	function openAll() {
		showAll = true;
		if (mdiList || mdiError) return;
		loadMdiIcons()
			.then(({ list }) => (mdiList = list))
			.catch(() => (mdiError = true));
	}

	function pick(name, path) {
		onchange(`mdi:${name}`, path);
	}

	/** Arrow keys inside a radio grid (roving tabindex). */
	function gridKey(e) {
		const keys = ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'];
		if (!keys.includes(e.key)) return;
		const grid = e.currentTarget.closest('[data-grid]');
		const items = [...root.querySelectorAll(`[data-grid="${grid.dataset.grid}"] [role="radio"]`)];
		const i = items.indexOf(e.currentTarget);
		const cols = Math.max(1, Math.round(grid.clientWidth / (items[0]?.offsetWidth || 40)));
		const next = { ArrowRight: i + 1, ArrowLeft: i - 1, ArrowDown: i + cols, ArrowUp: i - cols, Home: 0, End: items.length - 1 }[e.key];
		e.preventDefault();
		items[Math.max(0, Math.min(items.length - 1, next))]?.focus();
	}

	function commitSf() {
		const v = sfText.trim();
		if (!v) {
			if (value?.startsWith('sf:')) onchange(null, null);
			return;
		}
		if (validSf(v)) onchange(`sf:${v}`, null);
	}
</script>

<div class="tip" bind:this={root}>
	<input
		class="input"
		type="search"
		placeholder={t('tiles.iconSearchPlaceholder')}
		aria-label={t('tiles.iconSearch')}
		bind:value={query}
		data-testid="tile-icon-search"
	/>
	<div class="tip-scroll">
		{#each groups as g (g.id)}
			<p class="tip-cat" id="tipc-{g.id}">{g.label?.[locale] || g.label?.en}</p>
			<div class="tip-grid" role="radiogroup" aria-labelledby="tipc-{g.id}" data-grid="cur">
				{#each g.items as icon (icon.mdi)}
					{@const checked = current === icon.mdi}
					<button
						type="button"
						role="radio"
						aria-checked={checked ? 'true' : 'false'}
						aria-label={label(icon)}
						title="{label(icon)} · mdi:{icon.mdi} · SF {icon.sf}"
						tabindex={checked || (!current && icon === curated[0]) ? 0 : -1}
						onclick={() => pick(icon.mdi, icon.path)}
						onkeydown={gridKey}
						data-icon={icon.mdi}
					>
						<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d={icon.path} /></svg>
					</button>
				{/each}
			</div>
		{:else}
			<p class="t-help">{t('tiles.iconNoMatch')}</p>
		{/each}
	</div>
	<div class="tip-row">
		<button type="button" class="btn btn-sm btn-ghost" onclick={() => onchange(null, null)} aria-pressed={!value ? 'true' : 'false'}>{t('tiles.iconUseDefault')}</button>
		<button type="button" class="btn btn-sm btn-ghost" onclick={openAll} aria-expanded={showAll ? 'true' : 'false'} data-testid="tile-icon-all">{t('tiles.iconAllMdi')}</button>
	</div>
	{#if showAll}
		<div class="tip-all">
			{#if mdiError}
				<p class="field-error">{t('tiles.iconAllMdiFailed')}</p>
			{:else if !mdiList}
				<p class="t-help">{t('tiles.iconAllMdiLoading')}</p>
			{:else if !query.trim()}
				<p class="t-help">{t('tiles.iconAllMdiType')}</p>
			{:else}
				<div class="tip-grid all" role="radiogroup" aria-label={t('tiles.iconAllMdiResults')} data-grid="all">
					{#each allResults as icon, i (icon.name)}
						{@const checked = current === icon.name}
						<button
							type="button"
							role="radio"
							aria-checked={checked ? 'true' : 'false'}
							aria-label="mdi:{icon.name}"
							title="mdi:{icon.name}"
							tabindex={checked || (!allResults.some((x) => x.name === current) && i === 0) ? 0 : -1}
							onclick={() => pick(icon.name, icon.path)}
							onkeydown={gridKey}
						>
							<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d={icon.path} /></svg>
						</button>
					{/each}
				</div>
			{/if}
			<p class="t-help">{t('tiles.iconAllMdiHint')}</p>
		</div>
	{/if}
	<div class="field mt-2">
		<label class="field-label" for="tile-sf">{t('tiles.iconSf')}</label>
		<input
			id="tile-sf"
			class="input t-mono"
			placeholder={t('tiles.iconSfPlaceholder')}
			bind:value={sfText}
			onchange={commitSf}
			onkeydown={(e) => e.key === 'Enter' && (e.preventDefault(), commitSf())}
			aria-invalid={sfText && !validSf(sfText.trim()) ? 'true' : undefined}
			aria-describedby="tile-sf-hint"
			autocomplete="off"
			spellcheck="false"
		/>
		{#if sfText && !validSf(sfText.trim())}<p class="field-error">{t('tiles.iconSfInvalid')}</p>{/if}
		<p id="tile-sf-hint" class="t-help">{t('tiles.iconSfNote')}</p>
	</div>
</div>

<style>
	.tip {
		display: grid;
		gap: 8px;
		padding: 10px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		background: var(--surface-2);
	}
	.tip-scroll {
		max-height: 260px;
		overflow-y: auto;
		padding-right: 2px;
	}
	.tip-cat {
		margin: 8px 0 4px;
		font-size: var(--fs-cap);
		font-weight: 650;
		color: var(--ink-2);
	}
	.tip-cat:first-child {
		margin-top: 0;
	}
	.tip-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(40px, 1fr));
		gap: 4px;
	}
	.tip-grid button {
		display: grid;
		place-items: center;
		height: 40px;
		border-radius: var(--r-sm);
		border: 1px solid transparent;
		background: var(--surface);
		color: var(--ink);
	}
	.tip-grid button:hover {
		background: var(--fill-2);
	}
	.tip-grid button[aria-checked='true'] {
		border-color: var(--accent);
		background: var(--accent-soft);
		color: var(--accent-ink);
	}
	.tip-grid svg {
		width: 22px;
		height: 22px;
	}
	.tip-row {
		display: flex;
		gap: 6px;
		flex-wrap: wrap;
	}
	.tip-all {
		display: grid;
		gap: 6px;
	}
	.tip-grid.all {
		max-height: 180px;
		overflow-y: auto;
	}
	@media (pointer: coarse) {
		.tip-grid {
			grid-template-columns: repeat(auto-fill, minmax(44px, 1fr));
		}
		.tip-grid button {
			height: 44px;
		}
	}
</style>
