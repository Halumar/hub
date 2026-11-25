<script>
	/**
	 * Overflow menu ("More"): menu button pattern with arrow keys, Esc and
	 * focus return. The popup is position:fixed so cards with overflow:hidden
	 * do not clip it.
	 *
	 *   <Menu label="More" items={[{ label: 'Rename', icon: 'edit', onclick }, { separator: true },
	 *     { label: 'Delete', icon: 'trash', danger: true, onclick }]} />
	 */
	import Icon from './Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	let {
		items = [],
		label = '',
		ariaLabel = '',
		icon = 'more',
		buttonClass = 'btn btn-sm',
		align = 'end',
		testid = undefined,
		iconOnly = false,
	} = $props();

	const uid = `mn${Math.random().toString(36).slice(2, 8)}`;
	let open = $state(false);
	let btn;
	let menuEl = $state();
	let top = $state(0);
	let left = $state(0);

	function place() {
		const r = btn.getBoundingClientRect();
		const w = menuEl?.offsetWidth || 220;
		const h = menuEl?.offsetHeight || 200;
		left = align === 'end' ? Math.max(8, r.right - w) : Math.min(r.left, window.innerWidth - w - 8);
		top = r.bottom + 6 + h > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6;
	}

	function enabledItems() {
		return [...(menuEl?.querySelectorAll('[role="menuitem"]:not([disabled])') || [])];
	}

	async function show(focusLast = false) {
		open = true;
		await Promise.resolve();
		requestAnimationFrame(() => {
			place();
			const list = enabledItems();
			(focusLast ? list.at(-1) : list[0])?.focus();
		});
	}

	function hide(returnFocus = true) {
		if (!open) return;
		open = false;
		if (returnFocus) btn?.focus();
	}

	function onButtonKey(e) {
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			show();
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			show(true);
		}
	}

	function onMenuKey(e) {
		const list = enabledItems();
		const i = list.indexOf(document.activeElement);
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			hide();
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			list[(i + 1) % list.length]?.focus();
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			list[(i - 1 + list.length) % list.length]?.focus();
		} else if (e.key === 'Home') {
			e.preventDefault();
			list[0]?.focus();
		} else if (e.key === 'End') {
			e.preventDefault();
			list.at(-1)?.focus();
		} else if (e.key === 'Tab') {
			hide(false);
		}
	}

	function onWindowPointer(e) {
		if (!open) return;
		if (menuEl?.contains(e.target) || btn?.contains(e.target)) return;
		hide(false);
	}

	function run(item) {
		hide();
		item.onclick?.();
	}
</script>

<svelte:window onpointerdown={onWindowPointer} onresize={() => hide(false)} onscrollcapture={() => open && place()} />

<button
	bind:this={btn}
	type="button"
	class="{buttonClass} {iconOnly ? 'btn-icon' : ''}"
	aria-haspopup="menu"
	aria-expanded={open ? 'true' : 'false'}
	aria-controls={open ? uid : undefined}
	aria-label={ariaLabel || (iconOnly ? label || t('common.more') : undefined)}
	data-testid={testid}
	onclick={() => (open ? hide() : show())}
	onkeydown={onButtonKey}
>
	<Icon name={icon} />{#if !iconOnly}{label || t('common.more')}{/if}
</button>

{#if open}
	<div id={uid} class="menu" role="menu" tabindex="-1" bind:this={menuEl} style:top="{top}px" style:left="{left}px" onkeydown={onMenuKey}>
		{#each items.filter(Boolean) as item, i (i)}
			{#if item.separator}
				<div class="menu-sep" role="separator"></div>
			{:else}
				<button type="button" role="menuitem" class="menu-item {item.danger ? 'danger' : ''}" disabled={item.disabled} tabindex="-1" data-testid={item.testid} onclick={() => run(item)}>
					{#if item.icon}<Icon name={item.icon} />{/if}{item.label}
				</button>
			{/if}
		{/each}
	</div>
{/if}
