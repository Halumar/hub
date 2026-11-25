<script>
	/**
	 * Confirmation dialog on the shared Modal (focus trap, Esc, focus return).
	 * Focus starts on the safe action (Cancel). With `confirmText` the admin
	 * has to type that word first (danger zone).
	 *
	 *   <ConfirmDialog bind:open title=… message=… danger onconfirm={…} />
	 */
	import Modal from '$lib/ui/Modal.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	let {
		open = $bindable(false),
		title = '',
		message = '',
		confirmLabel = '',
		cancelLabel = '',
		danger = false,
		confirmText = '',
		onconfirm = () => {},
		oncancel = () => {},
	} = $props();

	let typed = $state('');
	const inputId = `cf${Math.random().toString(36).slice(2, 8)}`;
	$effect(() => {
		if (open) typed = '';
	});
	let canConfirm = $derived(!confirmText || typed.trim() === confirmText);

	function handleConfirm() {
		if (!canConfirm) return;
		open = false;
		onconfirm();
	}
</script>

<Modal bind:open title={title || t('common.confirm')} size="sm" onclose={oncancel} testid="confirm-dialog">
	{#if message}<p class="text-ink-2">{message}</p>{/if}
	{#if confirmText}
		<div class="field mt-4">
			<label class="field-label" for={inputId}>{t('common.typeToConfirm', { word: confirmText })}</label>
			<input id={inputId} class="input" bind:value={typed} autocomplete="off" spellcheck="false" data-autofocus />
		</div>
	{/if}
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} data-autofocus={confirmText ? undefined : true}>{cancelLabel || t('common.cancel')}</button>
		<button type="button" class="btn {danger ? 'btn-danger-solid' : 'btn-primary'}" onclick={handleConfirm} disabled={!canConfirm}>
			{confirmLabel || t('common.confirm')}
		</button>
	{/snippet}
</Modal>
