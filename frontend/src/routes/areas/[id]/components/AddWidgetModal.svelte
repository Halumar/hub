<script>
	/** "Add widget" dialog. Owns the new-widget form; the parent creates it. */
	import { WIDGET_TYPES } from '$lib/data/domains.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Field from '$lib/ui/Field.svelte';
	import WidgetFields from './WidgetFields.svelte';

	let { open = $bindable(false), sections = [], defaultSectionId = null, creating = false, oncreate } = $props();

	let widgetType = $state('clock');
	let config = $state({});
	let sectionId = $state(null);

	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen) {
			widgetType = 'clock';
			config = {};
			sectionId = defaultSectionId ?? sections[0]?.id ?? null;
		}
		wasOpen = open;
	});

	function submit(e) {
		e.preventDefault();
		if (!sectionId) return;
		oncreate?.({ section_id: sectionId, widget_type: widgetType, config });
	}
</script>

<Modal bind:open title={t('editor.addWidgetLong')} size="md" dismissible={!creating}>
	<form id="add-widget-form" class="stack" onsubmit={submit}>
		{#if sections.length > 1}
			<Field label={t('editor.widgetForm.section')} id="add-widget-section">
				{#snippet children({ id })}
					<select {id} class="select" bind:value={sectionId}>
						{#each sections as s (s.id)}<option value={s.id}>{s.title || t('editor.sectionUntitled')}</option>{/each}
					</select>
				{/snippet}
			</Field>
		{/if}
		<fieldset class="field">
			<legend class="field-label mb-1.5">{t('editor.widgetForm.type')}</legend>
			<div class="opts">
				{#each WIDGET_TYPES as wt (wt.value)}
					<button
						type="button"
						class="opt text-left"
						aria-pressed={widgetType === wt.value ? 'true' : 'false'}
						onclick={() => {
							widgetType = wt.value;
							config = {};
						}}
					>
						<span>{t(`editor.widgetTypes.${wt.value}.label`)}</span>
						<span class="t-help font-normal">{t(`editor.widgetTypes.${wt.value}.desc`)}</span>
					</button>
				{/each}
			</div>
		</fieldset>
		<WidgetFields type={widgetType} bind:config prefix="add-widget" />
	</form>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={creating}>{t('common.cancel')}</button>
		<button type="submit" form="add-widget-form" class="btn btn-primary" disabled={creating || !sectionId}>{creating ? t('common.adding') : t('editor.widgetForm.add')}</button>
	{/snippet}
</Modal>

<style>
	fieldset {
		border: 0;
		padding: 0;
		margin: 0;
	}
</style>
