<script>
	/** Widget settings fields shared by "Add widget" and "Edit widget". */
	import { t } from '$lib/i18n/index.svelte.js';
	import Field from '$lib/ui/Field.svelte';

	let { type, config = $bindable({}), prefix = 'w' } = $props();
	let jsonError = $state('');
</script>

{#if type === 'clock'}
	<label class="check">
		<input type="checkbox" checked={config.show_date !== false} onchange={(e) => (config.show_date = e.currentTarget.checked)} />
		{t('editor.widgetForm.showDate')}
	</label>
{:else if type === 'text'}
	<div class="stack">
		<Field label={t('editor.widgetForm.title')} id="{prefix}-title">
			{#snippet children({ id })}<input {id} class="input" bind:value={config.title} placeholder={t('editor.widgetForm.titlePlaceholder')} />{/snippet}
		</Field>
		<div class="grid grid-cols-2 gap-3">
			<Field label={t('editor.widgetForm.titleStyle')} id="{prefix}-style">
				{#snippet children({ id })}
					<select {id} class="select" bind:value={config.title_style}>
						{#each ['heading', 'title', 'subtitle'] as s (s)}<option value={s}>{t(`editor.widgetForm.styles.${s}`)}</option>{/each}
					</select>
				{/snippet}
			</Field>
			<Field label={t('editor.widgetForm.align')} id="{prefix}-align">
				{#snippet children({ id })}
					<select {id} class="select" bind:value={config.text_align}>
						{#each ['left', 'center', 'right'] as a (a)}<option value={a}>{t(`editor.widgetForm.aligns.${a}`)}</option>{/each}
					</select>
				{/snippet}
			</Field>
		</div>
		<Field label={t('editor.widgetForm.content')} hint={t('editor.widgetForm.contentHint')} id="{prefix}-content">
			{#snippet children({ id, describedby })}
				<textarea {id} class="textarea t-mono" rows="4" aria-describedby={describedby} bind:value={config.content} placeholder={t('editor.widgetForm.contentPlaceholder')}></textarea>
			{/snippet}
		</Field>
		<Field label={t('editor.widgetForm.color')} id="{prefix}-color">
			{#snippet children({ id })}<input {id} class="input" bind:value={config.text_color} placeholder={t('editor.widgetForm.colorPlaceholder')} />{/snippet}
		</Field>
	</div>
{:else if type !== 'spacer'}
	<Field label={t('editor.widgetForm.json')} error={jsonError} id="{prefix}-json">
		{#snippet children({ id, describedby, invalid })}
			<textarea
				{id}
				class="textarea t-mono"
				rows="5"
				aria-describedby={describedby}
				aria-invalid={invalid}
				value={JSON.stringify(config, null, 2)}
				oninput={(e) => {
					try {
						config = JSON.parse(e.currentTarget.value);
						jsonError = '';
					} catch {
						jsonError = t('editor.widgetForm.jsonInvalid');
					}
				}}
			></textarea>
		{/snippet}
	</Field>
{/if}
