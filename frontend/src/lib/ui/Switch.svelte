<script>
	/**
	 * On/off switch (button role="switch", aria-checked). The visible label is
	 * a <label for> so clicking the text toggles too.
	 *
	 *   <Switch bind:checked={on} label="Remote access" description="…" onchange={(v) => save(v)} />
	 *
	 * `onchange(next)` may return a Promise<boolean>; `false` reverts the switch.
	 */
	let {
		checked = $bindable(),
		label,
		description = '',
		disabled = false,
		labelHidden = false,
		onchange = null,
		testid = undefined,
		id = undefined,
		class: cls = '',
		children = null,
	} = $props();

	const autoId = `sw${Math.random().toString(36).slice(2, 9)}`;
	let sid = $derived(id || autoId);
	let busy = $state(false);

	async function toggle() {
		if (disabled || busy) return;
		const next = !checked;

		checked = next;
		if (!onchange) return;
		busy = true;
		try {
			const ok = await onchange(next);
			if (ok === false) checked = !next;
		} finally {
			busy = false;
		}
	}
</script>

<div class="switch-row {cls}">
	<button
		type="button"
		role="switch"
		id={sid}
		class="switch"
		aria-checked={checked ? 'true' : 'false'}
		aria-labelledby="{sid}-label"
		aria-describedby={description ? `${sid}-desc` : undefined}
		aria-busy={busy ? 'true' : undefined}
		{disabled}
		data-testid={testid}
		onclick={toggle}
	></button>
	<div class="min-w-0 flex-1 {labelHidden ? 'sr-only' : ''}">
		<label id="{sid}-label" for={sid} class="block font-medium">{label}</label>
		{#if description}<p id="{sid}-desc" class="t-help">{description}</p>{/if}
		{@render children?.()}
	</div>
</div>
