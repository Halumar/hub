<script>
	/** "Edit widget" dialog; open while `widget` is set. Emits the edited config. */
	import { t } from '$lib/i18n/index.svelte.js';
	import Modal from '$lib/ui/Modal.svelte';
	import WidgetFields from './WidgetFields.svelte';

	let { widget = null, saving = false, onsave, onclose } = $props();

	let config = $state({});
	let open = $state(false);
	let seededId = null;

	$effect(() => {
		if (widget && widget.id !== seededId) {
			seededId = widget.id;
			config = { ...widget.config };
			open = true;
		} else if (!widget) {
			seededId = null;
			open = false;
		}
	});

	function submit(e) {
		e.preventDefault();
		onsave?.(config);
	}
</script>

<Modal bind:open title={t('editor.editWidget')} description={widget ? t(`editor.widgetTypes.${widget.widget_type}.label`) : ''} size="md" dismissible={!saving} onclose={() => onclose?.()}>
	{#if widget}
		<form id="edit-widget-form" class="stack" onsubmit={submit}>
			<WidgetFields type={widget.widget_type} bind:config prefix="edit-widget" />
		</form>
	{/if}
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={saving}>{t('common.cancel')}</button>
		<button type="submit" form="edit-widget-form" class="btn btn-primary" disabled={saving}>{saving ? t('common.saving') : t('common.save')}</button>
	{/snippet}
</Modal>
