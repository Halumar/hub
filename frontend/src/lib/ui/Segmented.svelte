<script>
	/**
	 * Segmented control: a labeled group of toggle buttons (aria-pressed).
	 *
	 *   <Segmented label="Width" options={[{ value: 1, label: '1' }, …]} bind:value onchange={(v) => …} />
	 */
	let { options = [], value = $bindable(), label, disabled = false, onchange = null, testid = undefined, size = '' } = $props();

	function select(v) {
		if (disabled || v === value) return;
		value = v;
		onchange?.(v);
	}
</script>

<div class="seg {size}" role="group" aria-label={label} data-testid={testid}>
	{#each options as opt (opt.value)}
		<button
			type="button"
			aria-pressed={opt.value === value ? 'true' : 'false'}
			aria-label={opt.ariaLabel}
			title={opt.title}
			disabled={disabled || opt.disabled}
			onclick={() => select(opt.value)}
		>{opt.label}</button>
	{/each}
</div>
