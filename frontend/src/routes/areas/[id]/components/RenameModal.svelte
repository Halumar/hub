<script>
	/** Small rename dialog (sections, entities): replaces double-click inline editing. */
	import Modal from '$lib/ui/Modal.svelte';
	import Field from '$lib/ui/Field.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	let { open = $bindable(false), title = '', label = '', value = '', onsave } = $props();

	let draft = $state('');
	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen) draft = value || '';
		wasOpen = open;
	});

	function submit(e) {
		e.preventDefault();
		const v = draft.trim();
		if (!v) return;
		open = false;
		if (v !== value) onsave?.(v);
	}
</script>

<Modal bind:open {title} size="sm">
	<form id="rename-form" onsubmit={submit}>
		<Field {label} required id="rename-input">
			{#snippet children({ id })}
				<input {id} class="input" bind:value={draft} required autocomplete="off" />
			{/snippet}
		</Field>
	</form>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close}>{t('common.cancel')}</button>
		<button type="submit" form="rename-form" class="btn btn-primary" disabled={!draft.trim()}>{t('common.save')}</button>
	{/snippet}
</Modal>
