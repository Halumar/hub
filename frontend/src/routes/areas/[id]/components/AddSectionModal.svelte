<script>
	/** "Add section" dialog. Owns the title input; the parent creates the section. */
	import { t } from '$lib/i18n/index.svelte.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Field from '$lib/ui/Field.svelte';

	let { open = $bindable(false), creating = false, oncreate } = $props();

	let title = $state('');
	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen) title = '';
		wasOpen = open;
	});

	function submit(e) {
		e.preventDefault();
		oncreate?.(title);
	}
</script>

<Modal bind:open title={t('editor.addSectionLong')} size="sm" dismissible={!creating}>
	<form id="add-section-form" onsubmit={submit}>
		<Field label={t('editor.widgetForm.title')} id="add-section-title">
			{#snippet children({ id })}
				<input {id} class="input" bind:value={title} placeholder={t('editor.sectionDefaultTitle')} autocomplete="off" />
			{/snippet}
		</Field>
	</form>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={creating}>{t('common.cancel')}</button>
		<button type="submit" form="add-section-form" class="btn btn-primary" disabled={creating}>{creating ? t('common.creating') : t('editor.createSection')}</button>
	{/snippet}
</Modal>
