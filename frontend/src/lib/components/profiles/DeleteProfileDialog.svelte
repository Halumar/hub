<script>
	/**
	 * Delete a profile: devices that use it need a replacement profile
	 * (preview per device); default profiles and the last profile cannot be
	 * deleted.
	 */
	import Modal from '$lib/ui/Modal.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import PreviewView from './PreviewView.svelte';
	import LooseningWarning from './LooseningWarning.svelte';
	import { api } from '$lib/api.js';
	import { base } from '$app/paths';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { errorText } from '$lib/profiles/errors.js';
	import { shownPlatforms } from '$lib/device-info.js';

	let { open = $bindable(false), profile, profiles = [], ctx = {}, ondeleted = null } = $props();

	let replacement = $state('');
	let preview = $state(null);
	let confirmed = $state(false);
	let busy = $state(false);
	let error = $state('');

	$effect(() => {
		if (open) {
			replacement = '';
			preview = null;
			confirmed = false;
			error = '';
		}
	});

	let others = $derived(profiles.filter((p) => p.id !== profile?.id));
	let inUse = $derived((profile?.device_count || 0) > 0);
	let isDefault = $derived((profile?.default_for || []).length > 0);
	let isLast = $derived(others.length === 0);
	let blocked = $derived(isDefault || isLast);
	let ready = $derived(!blocked && (!inUse || !!replacement));
	// A preview belongs to the replacement it was asked for: a late answer for another one is dropped,
	// and deleting a profile in use needs the preview of exactly the current replacement.
	let previewFor = $state(null);
	let seq = 0;
	let current = $derived(!!preview && previewFor === replacement);
	function onReplacement() {
		seq += 1;
		busy = false;
		preview = null;
		confirmed = false;
	}

	function guestBlocked(p) {
		return (profile?.guest_device_count || 0) > 0 && p.child_protection;
	}

	async function loadPreview() {
		const asked = replacement;
		const s = ++seq;
		busy = true;
		error = '';
		try {
			const pv = await api.deleteProfile(profile.id, asked || null, { dryRun: true });
			if (s !== seq || asked !== replacement) return;
			preview = pv;
			previewFor = asked;
		} catch (e) {
			if (s === seq) error = errorText(e);
		} finally {
			if (s === seq) busy = false;
		}
	}

	async function del() {
		busy = true;
		error = '';
		try {
			await api.deleteProfile(profile.id, replacement || null);
			addToast(t('profiles.delete.done', { name: profile.name }), 'success');
			open = false;
			ondeleted?.();
		} catch (e) {
			error = errorText(e);
		} finally {
			busy = false;
		}
	}
</script>

<Modal bind:open title={t('profiles.delete.title', { name: profile?.name || '' })} size="lg" testid="delete-profile-dialog" dismissible={!busy}>
	<div class="stack">
		{#if isDefault}
			<Banner tone="warn" testid="delete-default">
				{t('profiles.delete.isDefault', { types: shownPlatforms(profile.default_for).map((s) => t(`profiles.slots.${s}`)).join(', ') })}
				{#snippet actions()}<a class="btn btn-sm" href="{base}/profiles#default-profiles" onclick={() => (open = false)}>{t('profiles.delete.changeDefault')}</a>{/snippet}
			</Banner>
		{:else if isLast}
			<Banner tone="warn">{t('profiles.err.last_profile')}</Banner>
		{:else if inUse}
			<p>{t('profiles.delete.inUse', { count: profile.device_count })}</p>
			<label class="field">
				<span class="field-label">{t('profiles.delete.replacement')}<span class="req" aria-hidden="true">*</span></span>
				<select class="select" bind:value={replacement} onchange={onReplacement} required data-testid="delete-replacement">
					<option value="" disabled>{t('profiles.delete.pick')}</option>
					{#each others as p (p.id)}
						<option value={p.id} disabled={guestBlocked(p)}>{p.name}{guestBlocked(p) ? ` – ${t('profiles.err.guestChildShort')}` : ''}</option>
					{/each}
				</select>
				<span class="t-help">{t('profiles.delete.replacementHint')}</span>
			</label>
		{:else}
			<p>{t('profiles.delete.unused')}</p>
		{/if}
		{#if error}<Banner tone="error">{error}</Banner>{/if}
		{#if current}
			<PreviewView {preview} {ctx} testid="delete-preview" />
			<LooseningWarning rules={preview.loosened_rules || []} bind:confirmed />
		{/if}
	</div>
	{#snippet footer({ close })}
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close} disabled={busy}>{t('common.cancel')}</button>
			{#if inUse && !current}
				<button type="button" class="btn btn-primary" onclick={loadPreview} disabled={busy || !ready} data-testid="delete-preview-btn">{t('profiles.preview.show')}</button>
			{:else}
				<button type="button" class="btn btn-danger-solid" onclick={del} disabled={busy || !ready || (inUse && !current) || (!!preview?.loosened_rules?.length && !confirmed)} data-testid="delete-confirm">{busy ? t('common.deleting') : t('profiles.delete.confirm')}</button>
			{/if}
		</div>
	{/snippet}
</Modal>
