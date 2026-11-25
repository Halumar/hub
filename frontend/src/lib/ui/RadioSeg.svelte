<script>
	/**
	 * Radio group as a segmented control (WAI-ARIA radio group): one tab stop,
	 * arrow keys move and select, Home/End jump. Use for exclusive choices whose
	 * value is part of a form (the tile panel); Segmented (aria-pressed) stays
	 * for immediate toggles.
	 *
	 *   <RadioSeg label="Layout" options={[{ value: 'compact', label: 'Compact' }, …]} value={v} onchange={(v) => …} />
	 */
	let { options = [], value, label = undefined, labelledby = undefined, onchange = null, testid = undefined, size = '', invalid = undefined, describedby = undefined } = $props();

	let root;

	function select(v) {
		if (v !== value) onchange?.(v);
	}

	function onkeydown(e, i) {
		const enabled = options.map((o, j) => (o.disabled ? -1 : j)).filter((j) => j >= 0);
		const pos = enabled.indexOf(i);
		let next = null;
		if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = enabled[(pos + 1) % enabled.length];
		else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = enabled[(pos - 1 + enabled.length) % enabled.length];
		else if (e.key === 'Home') next = enabled[0];
		else if (e.key === 'End') next = enabled[enabled.length - 1];
		if (next === null || next === undefined) return;
		e.preventDefault();
		select(options[next].value);
		root?.querySelectorAll('[role="radio"]')[next]?.focus();
	}

	let selectedIndex = $derived(Math.max(0, options.findIndex((o) => o.value === value && !o.disabled)));
</script>

<div
	class="seg {size}"
	role="radiogroup"
	aria-label={label}
	aria-labelledby={labelledby}
	aria-invalid={invalid}
	aria-describedby={describedby}
	data-testid={testid}
	bind:this={root}
>
	{#each options as opt, i (opt.value)}
		<button
			type="button"
			role="radio"
			aria-checked={opt.value === value ? 'true' : 'false'}
			tabindex={i === selectedIndex ? 0 : -1}
			title={opt.title}
			disabled={opt.disabled}
			onclick={() => select(opt.value)}
			onkeydown={(e) => onkeydown(e, i)}
		>{opt.label}</button>
	{/each}
</div>
