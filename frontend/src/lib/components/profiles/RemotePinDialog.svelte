<script>
	/**
	 * Set a remote PIN (8 digits) – for one device or for all devices of a
	 * profile. Entered twice, masked, never shown again; the hub only
	 * stores a hash.
	 */
	import Modal from '$lib/ui/Modal.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Field from '$lib/ui/Field.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { errorText } from '$lib/profiles/errors.js';

	let { open = $bindable(false), title, description = '', onsave, testid = 'pin-dialog' } = $props();

	let pin1 = $state('');
	let pin2 = $state('');
	let busy = $state(false);
	let error = $state('');
	let touched = $state(false);

	$effect(() => {
		if (open) {
			pin1 = '';
			pin2 = '';
			error = '';
			touched = false;
		}
	});

	let formatOk = $derived(/^\d{8}$/.test(pin1));
	let match = $derived(pin1 === pin2);
	let fieldError = $derived(!touched ? '' : !formatOk ? t('profiles.pin.format') : !match ? t('profiles.pin.mismatch') : '');

	async function save(e) {
		e?.preventDefault();
		touched = true;
		if (!formatOk || !match) return;
		busy = true;
		error = '';
		try {
			await onsave(pin1);
			pin1 = '';
			pin2 = '';
			open = false;
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Modal bind:open {title} {description} size="sm" {testid} dismissible={!busy}>
	<form id="{testid}-form" class="stack" onsubmit={save} autocomplete="off">
		<Field label={t('profiles.pin.new')} id="{testid}-1" hint={t('profiles.pin.hint')}>
			{#snippet children({ id, describedby })}
				<input {id} class="input input-mono" type="password" inputmode="numeric" maxlength="8" autocomplete="new-password" aria-describedby={describedby} bind:value={pin1} data-testid="{testid}-pin1" />
			{/snippet}
		</Field>
		<Field label={t('profiles.pin.repeat')} id="{testid}-2" error={fieldError}>
			{#snippet children({ id, describedby, invalid })}
				<input {id} class="input input-mono" type="password" inputmode="numeric" maxlength="8" autocomplete="new-password" aria-describedby={describedby} aria-invalid={invalid} bind:value={pin2} onblur={() => (touched = !!pin2)} data-testid="{testid}-pin2" />
			{/snippet}
		</Field>
		{#if error}<Banner tone="error">{error}</Banner>{/if}
	</form>
	{#snippet footer({ close })}
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close} disabled={busy}>{t('common.cancel')}</button>
			<button type="submit" form="{testid}-form" class="btn btn-primary" disabled={busy || !pin1 || !pin2} data-testid="{testid}-save">{busy ? t('common.saving') : t('profiles.pin.save')}</button>
		</div>
	{/snippet}
</Modal>
