<script>
	/** Advanced: HA users (info), integration/device gates, tokens, backup, danger zone. */
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { BRAND } from '$lib/brand.ts';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { copyText } from '$lib/utils.js';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import LoadError from '$lib/components/LoadError.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Modal from '$lib/ui/Modal.svelte';
	import { settings as S } from '../settings-state.svelte.js';

	const INTEGRATION_REPO_URL = `${BRAND.urls.source}/integration`;

	let newApiToken = $state('');
	let newIntegrationToken = $state('');
	let tokenBusy = $state({});
	let exporting = $state(false);
	let importing = $state(false);
	let importData = $state(null);
	let importOpen = $state(false);
	let deletingAreas = $state(false);
	let deletingDevices = $state(false);

	// Shared confirm dialog
	let confirm = $state({ open: false, title: '', message: '', label: '', danger: true, text: '', action: () => {} });
	function ask(opts) {
		confirm = { open: true, danger: true, text: '', ...opts };
	}

	async function setGate(key, next) {
		const r = await S.save(
			() =>
				key === 'pairingViaIntegration'
					? api.savePairingViaIntegration({ allow_pairing_via_integration: next })
					: api.saveComplianceEndpoint({ compliance_endpoint_enabled: next }),
			null,
			'settings.settingFailed',
		);
		if (!r) return false;
		const state = next ? t('settings.enabled') : t('settings.disabled');
		if (key === 'pairingViaIntegration') {
			S.pairingViaIntegration = r.allow_pairing_via_integration !== false;
			addToast(t('settings.pairingViaIntegrationSaved', { state }), 'success');
		} else {
			S.compliance = r.compliance_endpoint_enabled !== false;
			addToast(t('settings.complianceSaved', { state }), 'success');
		}
		return true;
	}

	// Who may administer the hub is only changed in the Home Assistant sidebar.
	const isIngress = typeof window !== 'undefined' && !!window.__INGRESS_PATH;

	async function setAdminDirect(next) {
		const r = await S.save(() => api.saveAdminDirectAccess({ admin_direct_access: next }), null, 'settings.settingFailed');
		if (!r) return false;
		S.adminDirectAccess = r.admin_direct_access === true;
		addToast(t('settings.adminDirectSaved', { state: next ? t('settings.enabled') : t('settings.disabled') }), 'success');
		return true;
	}

	function regenerate(kind) {
		const has = kind === 'api' ? S.hasApiToken : S.hasIntegrationToken;
		if (!has) return doRegenerate(kind);
		ask({
			title: kind === 'api' ? t('settings.regenTitle') : t('settings.regenIntTitle'),
			message: kind === 'api' ? t('settings.regenMessage') : t('settings.regenIntMessage'),
			label: t('settings.regenConfirm'),
			action: () => doRegenerate(kind),
		});
	}

	async function doRegenerate(kind) {
		tokenBusy = { ...tokenBusy, [kind]: true };
		try {
			if (kind === 'api') {
				const r = await api.regenerateApiToken();
				newApiToken = r.api_token;
				S.hasApiToken = true;
			} else {
				const r = await api.regenerateIntegrationToken();
				newIntegrationToken = r.integration_token;
				S.hasIntegrationToken = true;
			}
			addToast(t('settings.tokenGenerated'), 'warning');
		} catch (e) {
			addToast(t('settings.tokenFailed', { message: e.message }), 'error');
		} finally {
			tokenBusy = { ...tokenBusy, [kind]: false };
		}
	}

	async function copy(text) {
		const ok = await copyText(text);
		addToast(ok ? t('common.copied') : t('common.copyFailed'), ok ? 'success' : 'error');
	}

	async function exportAreas() {
		exporting = true;
		try {
			const data = await api.exportAreas();
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `hub-export-${new Date().toISOString().slice(0, 10)}.json`;
			a.click();
			URL.revokeObjectURL(url);
			addToast(t('settings.exported'), 'success');
		} catch (e) {
			addToast(t('settings.exportFailed', { message: e.message }), 'error');
		} finally {
			exporting = false;
		}
	}

	async function pickImport(event) {
		const input = event.currentTarget;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;
		try {
			const data = JSON.parse(await file.text());
			const areas = data.areas || data;
			importData = { areas, file: file.name, count: Array.isArray(areas) ? areas.length : 0 };
			importOpen = true;
		} catch (e) {
			addToast(t('settings.importFailed', { message: e.message }), 'error');
		}
	}

	async function runImport(overwrite) {
		const data = importData;
		importOpen = false;
		if (!data) return;
		importing = true;
		try {
			const r = await api.importAreas(data.areas, overwrite);
			// Import result: invalid entries are listed and not written.
			const failed = r.failed || 0;
			const errors = Array.isArray(r.errors) ? r.errors : [];
			let msg = failed
				? t('settings.importedFailed', { imported: r.imported, skipped: r.skipped, failed })
				: t('settings.imported', { imported: r.imported, skipped: r.skipped });
			if (errors.length) msg += `: ${errors.slice(0, 3).join('; ')}${errors.length > 3 ? ` (+${errors.length - 3})` : ''}`;
			addToast(msg, failed || errors.length ? 'warning' : 'success', failed || errors.length ? 10000 : 4000);
		} catch (e) {
			addToast(t('settings.importFailed', { message: e.message }), 'error');
		} finally {
			importing = false;
			importData = null;
		}
	}

	function deleteAllAreas() {
		ask({
			title: t('settings.deleteAreasTitle'),
			message: t('settings.deleteAreasMessage'),
			label: t('settings.deleteAreas'),
			text: t('common.confirmWord'),
			action: async () => {
				deletingAreas = true;
				try {
					await api.deleteAllAreas();
					addToast(t('settings.areasDeleted'), 'success');
				} catch (e) {
					addToast(t('settings.areasDeleteFailed', { message: e.message }), 'error');
				} finally {
					deletingAreas = false;
				}
			},
		});
	}

	function deleteAllDevices() {
		ask({
			title: t('settings.deleteDevicesTitle'),
			message: t('settings.deleteDevicesMessage', { count: S.clients.length }),
			label: t('settings.deleteDevices'),
			text: t('common.confirmWord'),
			action: async () => {
				deletingDevices = true;
				try {
					const r = await api.deleteAllClients();
					addToast(t('settings.devicesDeleted', { count: r?.deleted ?? 0 }), 'success');
					await S.loaders.clients();
				} catch (e) {
					addToast(t('settings.devicesDeleteFailed', { message: e.message }), 'error');
				} finally {
					deletingDevices = false;
				}
			},
		});
	}
</script>

{#snippet tokenCard(kind, title, hint, has, newToken, onDismiss)}
	<section class="card" aria-labelledby="tok-{kind}">
		<div class="card-head">
			<Icon name="key" />
			<h2 id="tok-{kind}" class="flex-1">{title}</h2>
			{#if kind === 'integration' && S.integrationSupervisorManaged}<span class="badge badge-ok">{t('settings.autoManaged')}</span>{/if}
		</div>
		<p class="t-help mb-3">{hint}</p>
		{#if S.loadErrors[kind === 'api' ? 'apiToken' : 'integrationToken']}
			<LoadError message={S.loadErrors[kind === 'api' ? 'apiToken' : 'integrationToken']} onretry={S.loaders[kind === 'api' ? 'apiToken' : 'integrationToken']} />
		{:else}
			<div class="stack">
				{#if kind === 'integration' && S.integrationSupervisorManaged}<Banner tone="ok">{t('settings.supervisorManaged')}</Banner>{/if}
				{#if newToken}
					<Banner tone="warn" title={t('settings.copyNow')}>
						<code class="t-mono block break-all select-all mt-1">{newToken}</code>
						{#snippet actions()}
							<button type="button" class="btn btn-sm" onclick={() => copy(newToken)}><Icon name="copy" />{t('common.copy')}</button>
							<button type="button" class="btn btn-sm btn-ghost" onclick={onDismiss}>{t('settings.savedIt')}</button>
						{/snippet}
					</Banner>
				{:else if has}
					<p class="row"><Icon name="shieldcheck" /><span>{t('settings.tokenSet')}</span></p>
				{/if}
				<div>
					<button type="button" class="btn" onclick={() => regenerate(kind)} disabled={tokenBusy[kind]}>
						<Icon name="refresh" />{tokenBusy[kind] ? t('settings.generating') : has ? t('settings.regenerate') : t('settings.generate')}
					</button>
				</div>
			</div>
		{/if}
	</section>
{/snippet}

<section class="card" data-testid="admin-access-card" aria-labelledby="adm-h">
	<div class="card-head"><Icon name="lock" /><h2 id="adm-h">{t('settings.adminAccess')}</h2></div>
	<Switch
		checked={S.adminDirectAccess}
		label={t('settings.adminDirect')}
		description={S.loadErrors.adminDirectAccess ? t('settings.valueUnknown') : isIngress ? t('settings.adminDirectHint') : `${t('settings.adminDirectHint')} ${t('settings.adminDirectIngressOnly')}`}
		disabled={!isIngress || !!S.loading.adminDirectAccess || !!S.loadErrors.adminDirectAccess}
		testid="admin-direct-switch"
		onchange={setAdminDirect}
	/>
</section>

<section class="card" data-testid="ha-users-card" aria-labelledby="hau-h">
	<div class="card-head"><Icon name="users" /><h2 id="hau-h">{t('settings.haUsers')}</h2></div>
	<p class="mb-2">{t('settings.haUsersText')} <a class="link" href={INTEGRATION_REPO_URL} target="_blank" rel="noopener noreferrer">{t('settings.integrationLink')}</a></p>
	<p class="t-help">{t('settings.haUsersRemote')}</p>
	<ul class="t-help list-disc pl-5 mt-1">
		{#each [1, 2, 3, 4] as i (i)}<li>{t(`settings.haUsersNote${i}`)}</li>{/each}
	</ul>
</section>

<section class="card" aria-labelledby="gates-h">
	<div class="card-head"><Icon name="shield" /><h2 id="gates-h">{t('settings.gates')}</h2></div>
	<div class="stack">
		<Switch
			checked={S.pairingViaIntegration}
			label={t('settings.pairingViaIntegration')}
			description={S.loadErrors.pairingViaIntegration ? t('settings.valueUnknown') : t('settings.pairingViaIntegrationHint')}
			disabled={!!S.loading.pairingViaIntegration || !!S.loadErrors.pairingViaIntegration}
			onchange={(v) => setGate('pairingViaIntegration', v)}
		/>
		<Switch
			checked={S.compliance}
			label={t('settings.compliance')}
			description={S.loadErrors.compliance ? t('settings.valueUnknown') : t('settings.complianceHint')}
			disabled={!!S.loading.compliance || !!S.loadErrors.compliance}
			onchange={(v) => setGate('compliance', v)}
		/>
		<Banner tone="neutral" icon="template" testid="profiles-moved">
			{t('settings.profilesMoved')}
			{#snippet actions()}<a class="btn btn-sm" href="{base}/profiles#default-profiles">{t('settings.openProfiles')}</a>{/snippet}
		</Banner>
	</div>
</section>

{@render tokenCard('api', t('settings.apiToken'), t('settings.apiTokenHint'), S.hasApiToken, newApiToken, () => (newApiToken = ''))}
{@render tokenCard('integration', t('settings.integrationToken'), t('settings.integrationTokenHint'), S.hasIntegrationToken, newIntegrationToken, () => (newIntegrationToken = ''))}

<section class="card" aria-labelledby="backup-h">
	<div class="card-head"><Icon name="download" /><h2 id="backup-h">{t('settings.backup')}</h2></div>
	<p class="t-help mb-3">{t('settings.backupHint')}</p>
	<div class="row">
		<button type="button" class="btn" onclick={exportAreas} disabled={exporting}><Icon name="download" />{exporting ? t('settings.exporting') : t('settings.exportJson')}</button>
		<label class="btn file-btn">
			<Icon name="upload" />{importing ? t('settings.importing') : t('settings.importJson')}
			<input type="file" class="sr-only" accept=".json,application/json" onchange={pickImport} disabled={importing} />
		</label>
	</div>
</section>

<section class="danger-zone stack" aria-labelledby="danger-h">
	<h2 id="danger-h">{t('settings.danger')}</h2>
	<div class="row justify-between">
		<div class="min-w-0 flex-1">
			<b>{t('settings.deleteDevices')}</b>
			<p class="t-help">{t('settings.deleteDevicesHint')}</p>
		</div>
		<button type="button" class="btn btn-danger" onclick={deleteAllDevices} disabled={deletingDevices || !S.clients.length}><Icon name="trash" />{deletingDevices ? t('common.deleting') : t('settings.deleteDevices')}</button>
	</div>
	<div class="row justify-between border-t border-line pt-3">
		<div class="min-w-0 flex-1">
			<b>{t('settings.deleteAreas')}</b>
			<p class="t-help">{t('settings.deleteAreasHint')}</p>
		</div>
		<button type="button" class="btn btn-danger" onclick={deleteAllAreas} disabled={deletingAreas}><Icon name="trash" />{deletingAreas ? t('common.deleting') : t('settings.deleteAreas')}</button>
	</div>
</section>

<ConfirmDialog bind:open={confirm.open} title={confirm.title} message={confirm.message} confirmLabel={confirm.label} danger={confirm.danger} confirmText={confirm.text} onconfirm={confirm.action} />

<Modal bind:open={importOpen} title={t('settings.importTitle')} size="sm" onclose={() => (importData = null)}>
	<p>{importData ? t('settings.importQuestion', { count: importData.count, file: importData.file }) : ''}</p>
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close}>{t('common.cancel')}</button>
		<button type="button" class="btn" onclick={() => runImport(false)} data-autofocus>{t('settings.keepExisting')}</button>
		<button type="button" class="btn btn-primary" onclick={() => runImport(true)}>{t('settings.overwrite')}</button>
	{/snippet}
</Modal>
