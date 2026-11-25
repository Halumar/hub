<script>
	/**
	 * Tabs (WAI-ARIA tabs pattern): role=tablist/tab/tabpanel, arrow keys,
	 * Home/End, automatic activation. Labels are always visible (also on
	 * phones, where the tab bar scrolls horizontally).
	 *
	 *   <Tabs items={[{ id: 'general', label: 'General', icon: 'gear' }]} bind:value onchange={…}>
	 *     …panel content for `value`…
	 *   </Tabs>
	 */
	import Icon from './Icon.svelte';

	let { items = [], value = $bindable(), label = '', onchange = null, children } = $props();
	const uid = `tb${Math.random().toString(36).slice(2, 8)}`;
	let listEl;

	function select(id, focus = false) {
		if (id !== value) {
			value = id;
			onchange?.(id);
		}
		if (focus) listEl?.querySelector(`#${uid}-${id}`)?.focus();
	}

	function onKeydown(e) {
		const i = items.findIndex((it) => it.id === value);
		let next = -1;
		if (e.key === 'ArrowRight') next = (i + 1) % items.length;
		else if (e.key === 'ArrowLeft') next = (i - 1 + items.length) % items.length;
		else if (e.key === 'Home') next = 0;
		else if (e.key === 'End') next = items.length - 1;
		if (next < 0) return;
		e.preventDefault();
		select(items[next].id, true);
	}
</script>

<div class="tabs" role="tablist" aria-label={label} bind:this={listEl}>
	{#each items as item (item.id)}
		<button
			type="button"
			role="tab"
			id="{uid}-{item.id}"
			class="tab"
			aria-selected={item.id === value ? 'true' : 'false'}
			aria-controls="{uid}-panel"
			tabindex={item.id === value ? 0 : -1}
			data-testid={item.testid}
			onclick={() => select(item.id)}
			onkeydown={onKeydown}
		>
			{#if item.icon}<Icon name={item.icon} />{/if}
			{item.label}
			{#if item.badge}<span class="badge badge-accent">{item.badge}</span>{/if}
		</button>
	{/each}
</div>
<div role="tabpanel" id="{uid}-panel" aria-labelledby="{uid}-{value}" tabindex="-1" class="focus-visible:outline-none">
	{@render children?.()}
</div>
