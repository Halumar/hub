<script>
	/**
	 * Removing a (non-guest) device. Online: plain confirmation, the app resets
	 * within the reset-ack window. Offline: the admin chooses when the
	 * device's HA user goes – on its next connect (max. 24 h, clean return to
	 * setup) or at once. Recommendation preselected: at once for devices with
	 * remote access, on reconnect for home-network-only devices. Lost or stolen
	 * devices are revoked instead (always immediate).
	 *
	 *   <DeleteDeviceDialog bind:open client={c} onremove={(mode) => …} onrevoke={() => …} />
	 *
	 * `onremove(mode)`: mode is null (online / revoked device) or
	 * 'on_reconnect' | 'immediate'. `onrevoke()`: the admin confirmed "Revoke".
	 */
	import Modal from '$lib/ui/Modal.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import { t } from '$lib/i18n/index.svelte.js';

	let { open = $bindable(false), client = null, busy = false, onremove = () => {}, onrevoke = () => {} } = $props();

	const uid = `dd${Math.random().toString(36).slice(2, 8)}`;
	let view = $state('delete');
	let removal = $state('on_reconnect');

	/** Offline, still active: its HA user is held until it returns. */
	let offline = $derived(!!client && !client.is_connected && !!client.is_active && !client.pending_deletion);
	// Unknown remote access (rules not resolved): the stronger removal, as with remote access.
	let remoteUnknown = $derived(client?.remote_allowed === null || client?.control_path === null);
	let recommended = $derived(client?.remote_allowed || remoteUnknown ? 'immediate' : 'on_reconnect');

	$effect(() => {
		if (open) {
			view = 'delete';
			removal = client?.remote_allowed || remoteUnknown ? 'immediate' : 'on_reconnect';
		}
	});

	function confirmRemove() {
		open = false;
		onremove(offline ? removal : null);
	}

	function confirmRevoke() {
		open = false;
		onrevoke();
	}

	const OPTIONS = [
		{ value: 'on_reconnect', label: 'devices.removeOnReconnect', hint: 'devices.removeOnReconnectHint' },
		{ value: 'immediate', label: 'devices.removeImmediate', hint: 'devices.removeImmediateHint' },
	];
</script>

<Modal
	bind:open
	title={view === 'revoke' ? t('devices.revokeTitle') : t('devices.confirmDeleteTitle')}
	size="sm"
	testid="delete-device-dialog"
>
	{#if !client}
		<!-- nothing -->
	{:else if view === 'revoke'}
		<p class="text-ink-2">{t('devices.revokeMessage', { name: client.name })}</p>
	{:else if !offline}
		<p class="text-ink-2">{t('devices.confirmDeleteMessage', { name: client.name })}</p>
	{:else}
		<div class="dd">
			<p class="text-ink-2">{t('devices.confirmDeleteOfflineMessage', { name: client.name })}</p>
			<Banner tone="info" testid="offline-delete-info" live={false}>{t('devices.offlineDeleteInfo')}</Banner>
			<fieldset class="choices" data-testid="ha-user-removal">
				<legend class="field-label">{t('devices.removalLegend')}</legend>
				{#each OPTIONS as o (o.value)}
					<label class="choice" class:on={removal === o.value}>
						<input type="radio" name="{uid}-removal" value={o.value} bind:group={removal} data-testid="removal-{o.value}" />
						<span class="cbody">
							<span class="clabel">
								{t(o.label)}
								{#if recommended === o.value}<span class="badge badge-accent" data-testid="removal-recommended">{t('devices.recommended')}</span>{/if}
							</span>
							<span class="t-help">{t(o.hint)}</span>
						</span>
					</label>
				{/each}
			</fieldset>
			<p class="t-help" data-testid="recommendation-reason">
				{remoteUnknown ? t('devices.recommendImmediateUnknownWhy') : client.remote_allowed ? t('devices.recommendImmediateWhy') : t('devices.recommendOnReconnectWhy')}
			</p>
		</div>
	{/if}
	{#if client && view === 'delete' && client.is_active && !client.pending_deletion}
		<p class="lost">
			{t('devices.lostQuestion')}
			<button type="button" class="linkbtn" onclick={() => (view = 'revoke')} data-testid="revoke-instead">→ {t('devices.revokeLink')}</button>
		</p>
	{/if}
	{#snippet footer({ close })}
		{#if view === 'revoke'}
			<button type="button" class="btn" onclick={() => (view = 'delete')} data-autofocus>{t('common.back')}</button>
			<button type="button" class="btn btn-danger-solid" onclick={confirmRevoke} disabled={busy} data-testid="confirm-revoke">{t('devices.revokeConfirm')}</button>
		{:else}
			<button type="button" class="btn" onclick={close} data-autofocus>{t('common.cancel')}</button>
			<button type="button" class="btn btn-danger-solid" onclick={confirmRemove} disabled={busy} data-testid="confirm-remove">{t('common.remove')}</button>
		{/if}
	{/snippet}
</Modal>

<style>
	.dd {
		display: grid;
		gap: 12px;
	}
	.choices {
		display: grid;
		gap: 8px;
		margin: 0;
		padding: 0;
		border: 0;
		min-width: 0;
	}
	.choices legend {
		margin-bottom: 6px;
		padding: 0;
	}
	.choice {
		display: flex;
		gap: 10px;
		align-items: flex-start;
		padding: 10px 12px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		background: var(--surface);
		cursor: pointer;
	}
	.choice.on {
		border-color: var(--ink);
		box-shadow: 0 0 0 1px var(--ink);
	}
	.choice:has(:focus-visible) {
		outline: 2px solid var(--focus);
		outline-offset: 2px;
	}
	.choice input {
		width: 18px;
		height: 18px;
		margin: 2px 0 0;
		flex: none;
		accent-color: var(--ink);
	}
	.cbody {
		display: grid;
		gap: 2px;
		min-width: 0;
	}
	.clabel {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
		font-weight: 650;
		color: var(--ink);
	}
	.lost {
		margin-top: 14px;
		font-size: var(--fs-sm);
		color: var(--ink-2);
	}
	.linkbtn {
		border: 0;
		background: none;
		padding: 0 2px;
		font: inherit;
		font-weight: 650;
		color: var(--err-ink);
		text-decoration: underline;
		text-underline-offset: 2px;
		cursor: pointer;
	}
	.linkbtn:focus-visible {
		outline: 2px solid var(--focus);
		border-radius: 4px;
	}
</style>
