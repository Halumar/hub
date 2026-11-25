<script>
	/** System: health, integration status, TLS, logs (wrapping, filter, auto refresh), restart. */
	import { onMount, onDestroy } from 'svelte';
	import { api } from '$lib/api.js';
	import { BRAND } from '$lib/brand.ts';
	import { copyText, formatDate } from '$lib/utils.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { wsConnection } from '$lib/stores/ws-store.svelte.js';
	import { t, i18n } from '$lib/i18n/index.svelte.js';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import WsBadge from '$lib/components/WsBadge.svelte';
	import AuditLog from './AuditLog.svelte';

	let health = $state({ status: '', version: '', build: '', uptime_seconds: 0, connected_clients: 0 });
	// AGPL-3.0-only, section 13: the admin UI names where its source code is.
	const SOURCE_URL = `${BRAND.urls.source}/hub`;
	let healthLoading = $state(true);
	let logs = $state('');
	let logsLoading = $state(false);
	let logLines = $state(200);
	let logFilter = $state('');
	let autoRefresh = $state(false);
	let logBox = $state(null);
	let restarting = $state(false);
	let tls = $state({ tls_enabled: false, tls_port: 8100, fingerprint: null, cert: null });
	let tlsLoading = $state(true);
	let tlsRegenerating = $state(false);
	let integration = $state({ connected: false, push_connected: false, last_request_ts: null, request_count: 0, auth_failure_count: 0, integration_version: null, addon_version: '', ha_version: null });
	let integrationLoading = $state(true);
	let nowTick = $state(Date.now());
	let healthInterval = null;
	let logsInterval = null;

	let confirm = $state({ open: false, title: '', message: '', label: '', action: () => {} });

	onMount(() => {
		loadHealth();
		loadLogs();
		loadTls();
		loadIntegration();
		healthInterval = setInterval(() => {
			loadHealth();
			loadIntegration();
			nowTick = Date.now();
		}, 10000);
	});

	onDestroy(() => {
		clearInterval(healthInterval);
		clearInterval(logsInterval);
	});

	$effect(() => {
		if (autoRefresh) {
			if (!logsInterval) logsInterval = setInterval(loadLogs, 10000);
		} else if (logsInterval) {
			clearInterval(logsInterval);
			logsInterval = null;
		}
	});

	async function loadHealth() {
		try {
			health = await api.health();
		} catch {
			/* keep the last values */
		} finally {
			healthLoading = false;
		}
	}

	async function loadLogs() {
		logsLoading = true;
		try {
			const r = await api.adminLogs(logLines);
			logs = r.logs || '';
			requestAnimationFrame(() => {
				if (logBox) logBox.scrollTop = logBox.scrollHeight;
			});
		} catch (e) {
			addToast(t('system.logsFailed', { message: e.message }), 'error');
		} finally {
			logsLoading = false;
		}
	}

	async function loadTls() {
		try {
			tls = await api.getTlsSettings();
		} catch {
			/* shown as unavailable */
		} finally {
			tlsLoading = false;
		}
	}

	async function loadIntegration() {
		try {
			integration = await api.integrationStatus();
		} catch {
			/* keep the last values */
		} finally {
			integrationLoading = false;
		}
	}

	function age(epochSeconds) {
		if (!epochSeconds) return t('common.never');
		void nowTick;
		const s = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
		const rtf = new Intl.RelativeTimeFormat(i18n.locale, { numeric: 'auto' });
		if (s < 60) return rtf.format(-s, 'second');
		if (s < 3600) return rtf.format(-Math.floor(s / 60), 'minute');
		if (s < 86400) return rtf.format(-Math.floor(s / 3600), 'hour');
		return rtf.format(-Math.floor(s / 86400), 'day');
	}

	function uptime(total) {
		const s = Math.max(0, Math.floor(total || 0));
		const d = Math.floor(s / 86400);
		const h = Math.floor((s % 86400) / 3600);
		const m = Math.floor((s % 3600) / 60);
		return [d && `${d} d`, (h || d) && `${h} h`, `${m} min`].filter(Boolean).join(' ');
	}

	let integrationState = $derived(
		integration.connected ? (integration.push_connected ? 'push' : 'poll') : integration.last_request_ts ? 'down' : 'never',
	);
	let certExpired = $derived(!!tls.cert?.not_valid_after && new Date(tls.cert.not_valid_after) < new Date());
	let fingerprintPairs = $derived(tls.fingerprint ? (tls.fingerprint.match(/.{1,2}/g) || []).join(':') : '');

	let logRows = $derived.by(() => {
		const q = logFilter.trim().toLowerCase();
		const rows = (logs || '').split('\n').filter((l) => l.length);
		return q ? rows.filter((l) => l.toLowerCase().includes(q)) : rows;
	});

	function levelOf(line) {
		if (/\b(ERROR|CRITICAL|Traceback)\b/.test(line)) return 'err';
		if (/\bWARN(ING)?\b/.test(line)) return 'warn';
		return '';
	}

	async function copyFingerprint() {
		if (tls.fingerprint && (await copyText(tls.fingerprint))) addToast(t('system.fingerprintCopied'), 'success');
	}

	function regenerateCert() {
		confirm = {
			open: true,
			title: t('system.regenCertTitle'),
			message: t('system.regenCertMessage'),
			label: t('system.regenCert'),
			action: async () => {
				tlsRegenerating = true;
				try {
					await api.regenerateTlsCert();
					await loadTls();
					addToast(t('system.certRegenerated'), 'warning');
				} catch (e) {
					addToast(t('system.certFailed', { message: e.message }), 'error');
				} finally {
					tlsRegenerating = false;
				}
			},
		};
	}

	function restart() {
		confirm = {
			open: true,
			title: t('system.restartTitle'),
			message: t('system.restartMessage'),
			label: t('system.restart'),
			action: async () => {
				restarting = true;
				try {
					await api.adminRestart();
					addToast(t('system.restartDone'), 'success');
				} catch (e) {
					addToast(t('system.restartFailed', { message: e.message }), 'error');
					restarting = false;
				}
			},
		};
	}
</script>

<section class="card" aria-labelledby="health-h">
	<div class="card-head"><Icon name="checkcircle" /><h2 id="health-h">{t('system.health')}</h2></div>
	<p class="t-help mb-3">{t('system.healthHint')}</p>
	{#if healthLoading}
		<LoadingState inline />
	{:else}
		<div class="kpis">
			<div class="box kpi"><span>{t('system.version')}</span><b class="t-mono">{health.version || '–'}</b>{#if health.build}<small>{health.build}</small>{/if}</div>
			<div class="box kpi"><span>{t('system.uptime')}</span><b>{uptime(health.uptime_seconds)}</b></div>
			<div class="box kpi"><span>{t('system.connectedDevices')}</span><b>{health.connected_clients ?? 0}</b><small>{t('system.connectedDevicesNote')}</small></div>
			<div class="box kpi"><span>{t('system.liveUpdates')}</span><b class="text-base"><WsBadge /></b><small class="t-mono">{wsConnection.status}</small></div>
		</div>
	{/if}
	<p class="t-help mt-3" data-testid="source-offer">{t('system.sourceOffer')} <a class="link" href={SOURCE_URL} target="_blank" rel="noopener noreferrer">{SOURCE_URL}</a></p>
</section>

<section class="card" aria-labelledby="int-h">
	<div class="card-head">
		<Icon name="link" />
		<h2 id="int-h" class="flex-1">{t('system.integration')}</h2>
		{#if !integrationLoading}
			<span class="badge {integration.connected ? 'badge-ok' : integration.last_request_ts ? 'badge-err' : ''}"><span class="dot"></span>{t(`system.integrationStates.${integrationState}`)}</span>
		{/if}
	</div>
	<p class="t-help mb-3">{t('system.integrationHint')} {integration.connected && integration.push_connected ? t('system.integrationPush') : t('system.integrationPoll')}</p>
	{#if integrationLoading}
		<LoadingState inline />
	{:else}
		<dl class="dl box">
			<dt>{t('system.lastRequest')}</dt><dd>{age(integration.last_request_ts)}</dd>
			<dt>{t('system.integrationVersion')}</dt><dd class="t-mono">{integration.integration_version || '–'}</dd>
			<dt>{t('system.haVersion')}</dt><dd class="t-mono" data-testid="ha-version">{integration.ha_version || '–'}</dd>
			<dt>{t('system.requests')}</dt><dd>{integration.request_count ?? 0}</dd>
			<dt>{t('system.authFailures')}</dt><dd class:t-err={integration.auth_failure_count > 0}>{integration.auth_failure_count ?? 0}</dd>
		</dl>
		{#if !integration.last_request_ts}
			<Banner tone="info" class="mt-3">
				{t('system.neverSeen')}
				{#snippet actions()}<a class="btn btn-sm" href="{BRAND.urls.source}/integration" target="_blank" rel="noopener noreferrer"><Icon name="external" />{t('system.getIntegration')}</a>{/snippet}
			</Banner>
		{:else if integration.auth_failure_count > 0 && !integration.connected}
			<Banner tone="warn" class="mt-3">{t('system.staleToken')}</Banner>
		{:else if integration.connected && !integration.push_connected}
			<Banner tone="info" class="mt-3">{t('system.noPush')}</Banner>
		{/if}
	{/if}
</section>

<section class="card" aria-labelledby="tls-h">
	<div class="card-head">
		<Icon name="lock" />
		<h2 id="tls-h" class="flex-1">{t('system.tls')}</h2>
		{#if !tlsLoading}
			<span class="badge {tls.tls_enabled ? 'badge-ok' : 'badge-warn'}">{tls.tls_enabled ? t('system.tlsOn', { port: tls.tls_port }) : t('system.tlsOff')}</span>
		{/if}
	</div>
	<p class="t-help mb-3">{t('system.tlsHint')}</p>
	{#if tlsLoading}
		<LoadingState inline />
	{:else}
		<div class="stack">
			{#if !tls.tls_enabled}<Banner tone="warn">{t('system.tlsOffHint', { port: tls.tls_port })}</Banner>{/if}
			{#if tls.fingerprint}
				<div class="field">
					<div class="row justify-between">
						<span class="field-label">{t('system.fingerprint')}</span>
						<button type="button" class="btn btn-sm" onclick={copyFingerprint}><Icon name="copy" />{t('system.copyFingerprint')}</button>
					</div>
					<code class="box t-mono break-all select-all">{fingerprintPairs}</code>
				</div>
				<dl class="dl">
					<dt>{t('system.validUntil')}</dt>
					<dd class:t-err={certExpired}>{formatDate(tls.cert?.not_valid_after)}{#if certExpired} ({t('system.expired')}){/if}</dd>
					<dt>{t('system.certType')}</dt>
					<dd>{tls.cert?.self_signed ? t('system.selfSigned') : t('system.caSigned')}{#if tls.cert?.subject} · {tls.cert.subject}{/if}</dd>
				</dl>
				<div>
					<button type="button" class="btn btn-danger" onclick={regenerateCert} disabled={tlsRegenerating}><Icon name="refresh" />{tlsRegenerating ? t('system.regenerating') : t('system.regenCert')}</button>
					<p class="t-help mt-1">{t('system.regenCertHint')}</p>
				</div>
			{:else if tls.tls_enabled}
				<p class="t-help">{t('system.noCert')}</p>
			{/if}
		</div>
	{/if}
</section>

<AuditLog />

<section class="card" aria-labelledby="logs-h">
	<div class="card-head">
		<Icon name="file" />
		<h2 id="logs-h" class="flex-1">{t('system.logs')}</h2>
		<button type="button" class="btn btn-sm" onclick={loadLogs} disabled={logsLoading}><Icon name="refresh" />{logsLoading ? t('system.refreshing') : t('system.refresh')}</button>
	</div>
	<p class="t-help mb-3">{t('system.logsHint')}</p>
	<div class="row mb-3">
		<label class="sr-only" for="log-filter">{t('system.logFilter')}</label>
		<input id="log-filter" class="input flex-1 min-w-[180px]" type="search" bind:value={logFilter} placeholder={t('system.logFilterPlaceholder')} />
		<label class="sr-only" for="log-lines">{t('system.lines')}</label>
		<select id="log-lines" class="select w-auto" bind:value={logLines} onchange={loadLogs}>
			{#each [100, 200, 500] as n (n)}<option value={n}>{t('system.linesN', { count: n })}</option>{/each}
		</select>
		<Switch bind:checked={autoRefresh} label={t('system.autoRefresh')} />
	</div>
	<div class="logs" bind:this={logBox} role="log" aria-label={t('system.logs')} data-testid="log-box">
		{#each logRows as line, i (i)}
			<div class="log {levelOf(line)}">{line}</div>
		{:else}
			<div class="t-help">{t('system.noLogs')}</div>
		{/each}
	</div>
</section>

<section class="danger-zone" aria-labelledby="restart-h">
	<h2 id="restart-h">{t('system.restart')}</h2>
	<p class="t-help mb-3">{t('system.restartHint')}</p>
	<button type="button" class="btn btn-danger" onclick={restart} disabled={restarting}><Icon name="power" />{restarting ? t('system.restarting') : t('system.restart')}</button>
</section>

<ConfirmDialog bind:open={confirm.open} title={confirm.title} message={confirm.message} confirmLabel={confirm.label} danger onconfirm={confirm.action} />

<style>
	.kpis {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
		gap: 10px;
	}
	.kpis .kpi b {
		font-size: 18px;
		margin-top: 2px;
	}
	.logs {
		max-height: 420px;
		overflow: auto;
		background: var(--surface-2);
		border-radius: var(--r-box);
		padding: 10px 12px;
		font-family: var(--mono);
		font-size: 12px;
		line-height: 1.55;
	}
	.log {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		border-bottom: 1px solid var(--line);
		padding: 2px 0;
	}
	.log:last-child {
		border-bottom: 0;
	}
	.log.warn {
		color: var(--warn-ink);
	}
	.log.err {
		color: var(--err-ink);
		font-weight: 600;
	}
</style>
