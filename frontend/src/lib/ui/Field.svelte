<script>
	/**
	 * Form field: <label for> linked to the control, hint and error linked via
	 * aria-describedby. The control is rendered by the caller:
	 *
	 *   <Field label="Name" required error={err} hint="…">
	 *     {#snippet children({ id, describedby, invalid })}
	 *       <input {id} class="input" aria-describedby={describedby} aria-invalid={invalid} required bind:value={name} />
	 *     {/snippet}
	 *   </Field>
	 */
	let { label, hint = '', error = '', required = false, id = undefined, class: cls = '', labelHidden = false, children, aside = null } = $props();

	const autoId = `f${Math.random().toString(36).slice(2, 9)}`;
	let fid = $derived(id || autoId);
	let hintId = $derived(hint ? `${fid}-hint` : '');
	let errId = $derived(error ? `${fid}-err` : '');
	let describedby = $derived([errId, hintId].filter(Boolean).join(' ') || undefined);
</script>

<div class="field {cls}">
	<div class="flex items-center justify-between gap-2">
		<label class="field-label {labelHidden ? 'sr-only' : ''}" for={fid}>
			{label}{#if required}<span class="req" aria-hidden="true">*</span>{/if}
		</label>
		{@render aside?.()}
	</div>
	{@render children({ id: fid, describedby, invalid: error ? 'true' : undefined })}
	{#if error}<p id={errId} class="field-error" role="alert">{error}</p>{/if}
	{#if hint}<p id={hintId} class="t-help">{hint}</p>{/if}
</div>
