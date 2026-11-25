<script>
	/**
	 * "New profile": three equal tiles – from a suggestion, from a
	 * device, empty. From a device copies the device's profile values with
	 * all its adjustments; "assign the device to the new profile" is
	 * preselected. Also opened from the device page (mode="device", client).
	 */
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import Modal from '$lib/ui/Modal.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { limits } from '$lib/profiles/catalog.svelte.js';
	import { errorText } from '$lib/profiles/errors.js';
	import { kindLabel, profileKinds } from '$lib/profiles/format.js';

	let { open = $bindable(false), mode = $bindable('choose'), client = null, clients = [], onsuggestion = null, oncreated = null } = $props();

	let name = $state('');
	let child = $state(false);
	let kind = $state('person');
	let deviceId = $state('');
	let assign = $state(true);
	let busy = $state(false);
	let error = $state('');

	$effect(() => {
		if (open) {
			name = client ? t('profiles.new.fromDeviceName', { name: client.name }).slice(0, limits().profile_name_max_length || 40) : '';
			child = false;
			kind = 'person';
			deviceId = client?.id || '';
			assign = true;
			error = '';
		}
	});

	const TILES = [
		{ id: 'suggestion', icon: 'sparkle' },
		{ id: 'device', icon: 'iphone' },
		{ id: 'empty', icon: 'plus' },
	];

	function choose(id) {
		if (id === 'suggestion') {
			open = false;
			onsuggestion?.();
			return;
		}
		mode = id;
	}

	let maxLen = $derived(limits().profile_name_max_length || 40);
	let valid = $derived(!!name.trim() && name.trim().length <= maxLen && (mode !== 'device' || !!deviceId));

	async function create(e) {
		e?.preventDefault();
		if (!valid) return;
		busy = true;
		error = '';
		try {
			const p =
				mode === 'device'
					? await api.profileFromDevice({ client_id: deviceId, name: name.trim(), assign })
					: await api.createProfile({ name: name.trim(), kind, child_protection: child, ...(child ? { rules: { control_path: kind === 'tv' ? 'checked' : 'hub' } } : {}) });
			addToast(t('profiles.new.created', { name: p.name }), 'success');
			open = false;
			if (oncreated) oncreated(p);
			else goto(`${base}/profiles/${p.id}`);
		} catch (err) {
			error = errorText(err);
		} finally {
			busy = false;
		}
	}
</script>

<Modal bind:open title={mode === 'device' ? t('profiles.new.fromDeviceTitle') : mode === 'empty' ? t('profiles.new.emptyTitle') : t('profiles.new.title')} size="lg" testid="new-profile-dialog" dismissible={!busy}>
	{#if mode === 'choose'}
		<div class="tiles3" data-eq="new-profile">
			{#each TILES as tile (tile.id)}
				<button type="button" class="ntile" onclick={() => choose(tile.id)} data-testid="new-{tile.id}">
					<Icon name={tile.icon} />
					<b>{t(`profiles.new.${tile.id}`)}</b>
					<small class="t-help">{t(`profiles.new.${tile.id}Hint`)}</small>
				</button>
			{/each}
		</div>
	{:else}
		<form id="new-profile-form" class="stack" onsubmit={create}>
			{#if mode === 'device' && !client}
				<label class="field">
					<span class="field-label">{t('profiles.new.device')}</span>
					<select class="select" bind:value={deviceId} required data-testid="new-device">
						<option value="" disabled>{t('profiles.new.pickDevice')}</option>
						{#each clients as c (c.id)}<option value={c.id}>{c.name} · {c.profile_name}</option>{/each}
					</select>
				</label>
			{/if}
			<Field label={t('profiles.name')} id="new-profile-name" required hint={t('profiles.nameHint', { max: maxLen })}>
				{#snippet children({ id, describedby })}
					<input {id} class="input" maxlength={maxLen} required aria-describedby={describedby} bind:value={name} data-testid="new-name" />
				{/snippet}
			</Field>
			{#if mode === 'empty'}
				<Field label={t('profiles.kind.label')} id="new-profile-kind" hint={t('profiles.kind.hint')}>
					{#snippet children({ id, describedby })}
						<select {id} class="select" aria-describedby={describedby} bind:value={kind} data-testid="new-kind">
							{#each profileKinds() as k (k.id)}<option value={k.id}>{kindLabel(k.id)}</option>{/each}
						</select>
					{/snippet}
				</Field>
				<Switch bind:checked={child} label={t('profiles.childProtection')} description={t('profiles.childProtectionHint')} testid="new-child">
					<p class="t-help" data-testid="new-child-pro"><span class="badge badge-accent">{t('profiles.pro')}</span> {t('profiles.childProtectionPro')}</p>
				</Switch>
				<p class="t-help">{t('profiles.new.emptyHint')}</p>
			{:else}
				<p class="t-help">{t('profiles.new.fromDeviceHint')}</p>
				<label class="check"><input type="checkbox" bind:checked={assign} data-testid="new-assign" />{t('profiles.new.assign')}</label>
			{/if}
			{#if error}<Banner tone="error">{error}</Banner>{/if}
		</form>
	{/if}
	{#snippet footer({ close })}
		<div class="foot-eq">
			{#if mode !== 'choose' && !client}
				<button type="button" class="btn" onclick={() => (mode = 'choose')} disabled={busy}>{t('common.back')}</button>
			{:else}
				<button type="button" class="btn" onclick={close} disabled={busy}>{t('common.cancel')}</button>
			{/if}
			{#if mode !== 'choose'}
				<button type="submit" form="new-profile-form" class="btn btn-primary" disabled={busy || !valid} data-testid="new-create">{busy ? t('common.saving') : t('profiles.new.create')}</button>
			{/if}
		</div>
	{/snippet}
</Modal>

<style>
	/* Equal sizes: three equal tiles. */
	.tiles3 {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		grid-auto-rows: 1fr;
		gap: 10px;
	}
	.ntile {
		display: grid;
		grid-template-rows: auto auto 1fr;
		justify-items: center;
		text-align: center;
		gap: 6px;
		padding: 16px 12px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		background: var(--surface);
		color: var(--ink);
		font: inherit;
		cursor: pointer;
		min-width: 0;
	}
	.ntile:hover {
		border-color: var(--ink-3);
	}
	.ntile :global(.ic) {
		width: 26px;
		height: 26px;
	}
	@media (max-width: 560px) {
		.tiles3 {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
