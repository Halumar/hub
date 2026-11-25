<script>
	/**
	 * Log settings: retention 7/30/90 days (nightly clean-up), hide
	 * people (activity only), the alarm threshold of rejections, CSV export
	 * and the privacy notes.
	 */
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';
	import { t, i18n } from '$lib/i18n/index.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LogHeader from '$lib/components/log/LogHeader.svelte';
	import ExportDialog from '$lib/components/log/ExportDialog.svelte';
	import { setNamespace } from '$lib/log/describe.js';

	const THRESHOLDS = [
		[3, 10],
		[5, 10],
		[10, 60],
	];

	let summary = $state(null);
	let settings = $state(null);
	let loading = $state(true);
	let error = $state('');
	let saving = $state(false);
	let exportOpen = $state(false);

	onMount(load);

	async function load() {
		try {
			const [s, st] = await Promise.all([api.getLogSummary(), api.getLogSettings()]);
			summary = s;
			setNamespace(s?.namespace);
			settings = st;
			error = '';
		} catch (e) {
			error = t('log.loadFailed', { message: e.message });
		} finally {
			loading = false;
		}
	}

	async function save(patch, toast) {
		saving = true;
		try {
			settings = await api.saveLogSettings({ ...$state.snapshot(settings), ...patch });
			summary = await api.getLogSummary();
			addToast(toast || t('log.saved'), 'success');
		} catch (e) {
			addToast(t('log.saveFailed', { message: e.message }), 'error');
		} finally {
			saving = false;
		}
	}

	function setRetention(days) {
		if (days === settings.retention_days) return;
		save({ retention_days: days }, t('log.retentionSaved', { days }));
	}

	function toggleHidden(owner, checked) {
		const list = checked ? [...settings.hidden_owners, owner] : settings.hidden_owners.filter((o) => o !== owner);
		save({ hidden_owners: list });
	}

	let owners = $derived([...new Set([...(summary?.facets?.owners || []), ...(settings?.hidden_owners || [])])].sort((a, b) => a.localeCompare(b)));
</script>

<svelte:head><title>{t('log.tabSettings')} · {t('log.title')}</title></svelte:head>

<div class="page logpage" data-testid="logsettings-page" lang={i18n.locale}>
	<LogHeader active="settings" {summary} />
	{#if loading}
		<LoadingState />
	{:else if error}
		<ErrorState message={error} onretry={load} />
	{:else}
		<div class="stack">
			<section class="card stack-sm" aria-labelledby="h-ret">
				<div class="card-head"><Icon name="history" /><h2 id="h-ret">{t('log.retention')}</h2></div>
				<div class="seg even full ret" role="group" aria-labelledby="h-ret" data-eq="retention" data-testid="log-retention">
					{#each [7, 30, 90] as d (d)}
						<button type="button" aria-pressed={settings.retention_days === d} onclick={() => setRetention(d)} disabled={saving}>{d === 30 ? t('log.retentionDefault', { days: d }) : t('log.retentionDays', { days: d })}</button>
					{/each}
				</div>
				<span class="t-help">{t('log.retentionHelp')}</span>
				{#if settings.retention_days === 7}
					<div class="banner banner-warn"><Icon name="warn" /><div class="banner-body">{t('log.retentionShort', { days: 7 })}</div></div>
				{:else if settings.retention_days === 90}
					<div class="banner banner-neutral"><Icon name="info" /><div class="banner-body">{t('log.retentionLong')}</div></div>
				{/if}
			</section>

			<section class="card stack-sm" aria-labelledby="h-hide">
				<div class="card-head"><Icon name="eyeoff" /><h2 id="h-hide">{t('log.hidePeople')}</h2></div>
				<p class="t-sub">{t('log.hidePeopleText')}</p>
				{#if owners.length}
					<fieldset class="hidegrid" data-testid="log-hide">
						<legend class="sr-only">{t('log.person')}</legend>
						{#each owners as o (o)}
							<label class="check"><input type="checkbox" checked={settings.hidden_owners.includes(o)} onchange={(e) => toggleHidden(o, e.currentTarget.checked)} disabled={saving} /><span>{o}</span></label>
						{/each}
					</fieldset>
				{:else}
					<p class="t-help">{t('log.hidePeopleNone')}</p>
				{/if}
				{#if settings.hidden_owners.length}<span class="t-help">{t('log.hidePeopleNote')}</span>{/if}
			</section>

			<section class="card stack-sm" aria-labelledby="h-thr">
				<div class="card-head"><Icon name="lock" /><h2 id="h-thr">{t('log.alarmThreshold')}</h2></div>
				<p class="t-sub">{t('log.alarmThresholdText')}</p>
				<div class="seg even full ret" role="group" aria-labelledby="h-thr" data-eq="threshold">
					{#each THRESHOLDS as [count, minutes] (count)}
						<button
							type="button"
							aria-pressed={settings.alarm_threshold.count === count && settings.alarm_threshold.window_minutes === minutes}
							onclick={() => save({ alarm_threshold: { count, window_minutes: minutes } })}
							disabled={saving}>{t('log.thresholdOption', { count, minutes })}</button
						>
					{/each}
				</div>
			</section>

			<section class="card stack-sm" aria-labelledby="h-exp">
				<div class="card-head"><Icon name="download" /><h2 id="h-exp">{t('log.export')}</h2></div>
				<p class="t-sub">{t('log.exportText')}</p>
				<div class="btnrow" style="width:min(100%,260px)"><button type="button" class="btn" onclick={() => (exportOpen = true)} data-testid="log-export-open"><Icon name="download" />{t('log.exportButton')}</button></div>
			</section>

			<section class="card stack-sm" aria-labelledby="h-priv">
				<div class="card-head"><Icon name="shield" /><h2 id="h-priv">{t('log.privacy')}</h2></div>
				<ul class="plist">
					<li><Icon name="user" /><span>{@html t('log.privacyAdmins')}</span></li>
					<li><Icon name="home" /><span>{@html t('log.privacyLocal')}</span></li>
					<li><Icon name="send" /><span>{t('log.privacyNotify')}</span></li>
					<li><Icon name="lock" /><span>{t('log.privacySecrets')}</span></li>
				</ul>
				<p class="t-help" data-testid="log-stored">{t('log.stored', { count: summary?.total || 0, days: settings.retention_days })}</p>
			</section>
		</div>
		<ExportDialog bind:open={exportOpen} total={summary?.total || 0} hidden={settings.hidden_owners} />
	{/if}
</div>

<style>
	.ret {
		max-width: 520px;
	}
	.hidegrid {
		border: 0;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
		gap: var(--s2);
	}
	.plist {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 8px;
		font-size: var(--fs-sm);
	}
	.plist li {
		display: flex;
		gap: 10px;
		align-items: flex-start;
	}
	.plist :global(.ic) {
		width: 16px;
		height: 16px;
		flex: none;
		margin-top: 2px;
		color: var(--ink-2);
	}
	@media (max-width: 639px) {
		.ret button {
			min-height: 44px;
		}
	}
</style>
