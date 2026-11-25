<script>
	/**
	 * Assign a profile to one device with preview: pick a profile,
	 * optionally drop all adjustments, see what changes (dry run), confirm
	 * loosened security rules, then save. `reset` = same profile, all
	 * adjustments dropped ("Reset everything to the profile").
	 */
	import Modal from '$lib/ui/Modal.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import ProfilePicker from './ProfilePicker.svelte';
	import PreviewView from './PreviewView.svelte';
	import LooseningWarning from './LooseningWarning.svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { errorText } from '$lib/profiles/errors.js';

	let { open = $bindable(false), client, profiles = [], ctx = {}, reset = false, onassigned = null } = $props();

	let profileId = $state(null);
	let discard = $state(false);
	let preview = $state(null);
	let confirmed = $state(false);
	let busy = $state(false);
	let error = $state('');

	$effect(() => {
		if (open) {
			profileId = client?.profile_id ?? null;
			discard = reset;
			preview = null;
			confirmed = false;
			error = '';
		}
	});

	let isGuest = $derived(client?.access === 'guest');
	let changed = $derived(reset || profileId !== client?.profile_id || discard);
	let needsConfirm = $derived(!!preview?.loosened_rules?.length && !confirmed);
	// A preview belongs to the inputs it was asked for: a late answer for other inputs is dropped,
	// and saving needs the preview of exactly the current inputs.
	let inputKey = $derived(JSON.stringify([profileId, discard]));
	let previewKey = $state('');
	let seq = 0;
	let current = $derived(!!preview && previewKey === inputKey);

	function guestReason(p) {
		return isGuest && p.child_protection ? t('profiles.err.guestChildShort') : '';
	}

	async function loadPreview() {
		const key = inputKey;
		const s = ++seq;
		busy = true;
		error = '';
		try {
			const pv = await api.assignClientProfile(client.id, { profile_id: profileId, discard_overrides: discard }, { dryRun: true });
			if (s !== seq || key !== inputKey) return;
			preview = pv;
			previewKey = key;
		} catch (e) {
			if (s === seq) error = errorText(e);
		} finally {
			if (s === seq) busy = false;
		}
	}

	async function save() {
		busy = true;
		error = '';
		try {
			const updated = await api.assignClientProfile(client.id, { profile_id: profileId, discard_overrides: discard });
			const name = profiles.find((p) => p.id === profileId)?.name || '';
			addToast(reset ? t('profiles.assign.resetDone') : t('profiles.assign.done', { name }), 'success');
			open = false;
			onassigned?.(updated);
		} catch (e) {
			error = errorText(e);
		} finally {
			busy = false;
		}
	}

	function onPick() {
		seq += 1;
		busy = false;
		preview = null;
		confirmed = false;
	}
</script>

<Modal bind:open title={reset ? t('profiles.assign.resetTitle') : t('profiles.assign.title', { name: client?.name || '' })} description={reset ? t('profiles.assign.resetHint') : t('profiles.assign.hint')} size="lg" testid="assign-dialog" dismissible={!busy}>
	<div class="stack">
		{#if !reset}
			<ProfilePicker {profiles} bind:value={profileId} label={t('profiles.assign.pick')} disabledReason={guestReason} onchange={onPick} name="assign-profile" />
			{#if client?.override_count}
				<label class="check">
					<input type="checkbox" bind:checked={discard} onchange={onPick} data-testid="assign-discard" />
					{t('profiles.assign.discard', { count: client.override_count })}
				</label>
			{/if}
		{/if}
		{#if error}<Banner tone="error">{error}</Banner>{/if}
		{#if current}
			<PreviewView {preview} {ctx} testid="assign-preview" />
			<LooseningWarning rules={preview.loosened_rules || []} bind:confirmed subject={client?.name} />
		{/if}
	</div>
	{#snippet footer({ close })}
		<div class="foot-eq">
		<button type="button" class="btn" onclick={close} disabled={busy}>{t('common.cancel')}</button>
		{#if !current}
			<button type="button" class="btn btn-primary" onclick={loadPreview} disabled={busy || !changed || !profileId} data-testid="assign-preview-btn">{busy ? t('common.loading') : t('profiles.preview.show')}</button>
		{:else}
			<button type="button" class="btn btn-primary" onclick={save} disabled={busy || !current || needsConfirm} data-testid="assign-save">{busy ? t('common.saving') : reset ? t('profiles.assign.resetConfirm') : t('profiles.assign.confirm')}</button>
		{/if}
		</div>
	{/snippet}
</Modal>
