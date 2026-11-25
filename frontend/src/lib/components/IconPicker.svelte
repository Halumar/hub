<script>
	/**
	 * IconPicker – MDI icon search + free text.
	 *
	 * One text field that accepts any value (e.g. "sf:house.fill" for SF
	 * Symbols). While typing it searches the MDI library and shows matches with
	 * previews; picking one fills "mdi:<name>". Keyboard: ArrowDown moves into
	 * the results, Esc closes them, Enter commits the typed value.
	 * The MDI catalog (~2.7 MB) is a lazy chunk, fetched only on focus or for
	 * the preview of an "mdi:" value.
	 *
	 * Props: value, onchange(value), id (for an external <label for>)
	 */
	import { loadMdiIcons } from '$lib/data/mdi-lazy.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';

	let { value = '', onchange = () => {}, id = undefined } = $props();

	let mdiIcons = $state(null);
	let iconMap = $state(null);
	let iconsError = $state(false);
	let inputValue = $state('');
	let focused = $state(false);
	let showDropdown = $state(false);
	let root;
	const listId = `ip${Math.random().toString(36).slice(2, 8)}`;

	function ensureIcons() {
		if (mdiIcons || iconsError) return;
		loadMdiIcons()
			.then(({ list, map }) => {
				mdiIcons = list;
				iconMap = map;
			})
			.catch(() => {
				iconsError = true;
			});
	}

	$effect(() => {
		if (value?.startsWith('mdi:')) ensureIcons();
	});
	$effect(() => {
		inputValue = value;
	});

	let results = $derived.by(() => {
		if (!focused || !inputValue || !mdiIcons) return [];
		const q = inputValue.replace(/^mdi:/, '').toLowerCase().trim();
		if (!q) return [];
		const matches = [];
		for (const [name, path] of mdiIcons) {
			if (name.includes(q)) {
				matches.push({ name, path });
				if (matches.length >= 60) break;
			}
		}
		return matches;
	});

	let currentPath = $derived(value?.startsWith('mdi:') && iconMap ? iconMap.get(value.slice(4)) || null : null);

	function commit() {
		if (inputValue !== value) onchange(inputValue);
	}

	function selectIcon(name) {
		inputValue = `mdi:${name}`;
		showDropdown = false;
		onchange(inputValue);
		root?.querySelector('input')?.focus();
	}

	function onFocusOut(e) {
		if (root?.contains(e.relatedTarget)) return;
		focused = false;
		showDropdown = false;
		commit();
	}

	function onKeydown(e) {
		if (e.key === 'Escape' && showDropdown) {
			e.preventDefault();
			e.stopPropagation();
			showDropdown = false;
		} else if (e.key === 'Enter') {
			e.preventDefault();
			showDropdown = false;
			commit();
		} else if (e.key === 'ArrowDown' && results.length) {
			e.preventDefault();
			showDropdown = true;
			requestAnimationFrame(() => root?.querySelector('.ip-result')?.focus());
		}
	}

	function onResultKey(e) {
		const items = [...root.querySelectorAll('.ip-result')];
		const i = items.indexOf(e.currentTarget);
		const cols = 6;
		let next = -1;
		if (e.key === 'ArrowRight') next = i + 1;
		else if (e.key === 'ArrowLeft') next = i - 1;
		else if (e.key === 'ArrowDown') next = i + cols;
		else if (e.key === 'ArrowUp') next = i - cols;
		else if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			showDropdown = false;
			root.querySelector('input')?.focus();
			return;
		}
		if (next === -1 && !['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'].includes(e.key)) return;
		e.preventDefault();
		if (next < 0) root.querySelector('input')?.focus();
		else items[Math.min(next, items.length - 1)]?.focus();
	}

	function clear() {
		inputValue = '';
		onchange('');
		showDropdown = false;
	}
</script>

<div class="ip" bind:this={root} onfocusout={onFocusOut}>
	<div class="flex items-center gap-2">
		{#if currentPath}
			<svg class="ip-preview" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d={currentPath} /></svg>
		{/if}
		<input
			{id}
			type="text"
			class="input"
			value={inputValue}
			role="combobox"
			aria-expanded={showDropdown && results.length > 0 ? 'true' : 'false'}
			aria-controls={listId}
			aria-autocomplete="list"
			aria-label={id ? undefined : t('areas.iconPicker.label')}
			placeholder={t('areas.iconPicker.placeholder')}
			autocomplete="off"
			oninput={(e) => {
				inputValue = e.currentTarget.value;
				showDropdown = true;
			}}
			onfocus={() => {
				ensureIcons();
				focused = true;
				showDropdown = true;
			}}
			onkeydown={onKeydown}
		/>
		{#if value}
			<button type="button" class="btn btn-ghost btn-icon btn-sm" onclick={clear} aria-label={t('areas.iconPicker.clear')}>
				<Icon name="x" />
			</button>
		{/if}
	</div>

	{#if showDropdown && results.length > 0}
		<div class="ip-pop" id={listId} role="group" aria-label={t('areas.iconPicker.results')}>
			<div class="ip-grid">
				{#each results as icon (icon.name)}
					<button
						type="button"
						class="ip-result"
						aria-pressed={value === `mdi:${icon.name}` ? 'true' : 'false'}
						title={`mdi:${icon.name}`}
						onmousedown={(e) => e.preventDefault()}
						onclick={() => selectIcon(icon.name)}
						onkeydown={onResultKey}
					>
						<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d={icon.path} /></svg>
						<span>{icon.name}</span>
					</button>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.ip {
		position: relative;
	}
	.ip-preview {
		width: 24px;
		height: 24px;
		flex: none;
		color: var(--ink);
	}
	.ip-pop {
		position: absolute;
		left: 0;
		right: 0;
		top: calc(100% + 4px);
		z-index: 60;
		background: var(--surface);
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		box-shadow: var(--shadow-pop);
		max-height: 280px;
		overflow-y: auto;
	}
	.ip-grid {
		display: grid;
		grid-template-columns: repeat(6, minmax(0, 1fr));
		gap: 2px;
		padding: 6px;
	}
	.ip-result {
		display: grid;
		justify-items: center;
		gap: 2px;
		padding: 6px 2px;
		min-height: 44px;
		border: 1px solid transparent;
		border-radius: var(--r-sm);
		background: none;
		color: var(--ink);
	}
	.ip-result:hover {
		background: var(--fill-2);
	}
	.ip-result[aria-pressed='true'] {
		background: var(--accent-soft);
		border-color: var(--accent);
	}
	.ip-result svg {
		width: 20px;
		height: 20px;
	}
	.ip-result span {
		font-size: 10px;
		color: var(--ink-3);
		width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	@media (max-width: 480px) {
		.ip-grid {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
	}
</style>
