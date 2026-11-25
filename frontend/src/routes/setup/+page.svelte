<script>
	/**
	 * Setup assistant: integration → connection/token → addresses →
	 * first area → first device → next steps. Skippable, progress stored in
	 * the hub, steps done elsewhere are recognized from the live data.
	 */
	import { onMount, onDestroy, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { BRAND } from '$lib/brand.ts';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { openPairing } from '$lib/stores/pairing.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { debounce } from '$lib/utils.js';
	import { isValidUrl } from '../settings/settings-state.svelte.js';
	import {
		STEPS,
		MIN_HA_VERSION,
		myHaAddIntegration,
		APP_STORE_URL,
		isPlaceholderUrl,
		haVersionSupported,
		doneSteps,
		firstOpenStep, myHaIntegrationRepository, INSTALL_GUIDE_PATH } from '$lib/setup.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Field from '$lib/ui/Field.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import BrandMark from '$lib/ui/BrandMark.svelte';
	import { syncProblems } from '$lib/sync-result.js';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import StatusBadge from '$lib/ui/StatusBadge.svelte';
	import { defaultProfilesSummary } from '$lib/device-info.js';

	const NEXT = [
		{ key: 'guests', icon: 'users', href: '/guests' },
		{ key: 'profiles', icon: 'template', href: '/profiles' },
		{ key: 'panel', icon: 'ipad', href: '/clients', pro: true },
		{ key: 'areas', icon: 'grid', href: '/areas' },
	];

	let loading = $state(true);
	let loadError = $state('');
	let progress = $state({ status: 'in_progress', step: 'integration', skipped: [] });

	let integration = $state(null);
	let token = $state(null);
	let haUrls = $state({});
	let urlEdit = $state({ ha_url: '', ha_internal_url: '' });
	let urlErrors = $state({ ha_url: '', ha_internal_url: '' });
	let areas = $state([]);
	let clients = $state([]);
	let profileDefaults = $state('');

	let haAreas = $state(null);
	let haAreasError = $state('');
	let selected = $state(new Set());
	let withEntities = $state(true);
	let newName = $state('');
	let busy = $state({});
	let checking = $state(false);

	let heading = $state();
	let poll;
	let unsubs = [];

	let done = $derived(doneSteps({ integration, token, haUrls, areas, devices: clients }));
	let index = $derived(Math.max(0, STEPS.indexOf(progress.step)));
	let step = $derived(STEPS[index]);
	let members = $derived(clients.filter((c) => !c.pending_deletion && c.access !== 'guest'));
	let haSupported = $derived(haVersionSupported(integration?.ha_version));
	let importedHa = $derived(new Set(areas.map((a) => a.ha_area_id).filter(Boolean)));
	let openCount = $derived(STEPS.slice(0, -1).filter((s) => !done[s]).length);

	function stateOf(s) {
		if (done[s]) return 'done';
		if (progress.skipped.includes(s)) return 'skipped';
		return 'open';
	}

	onMount(async () => {
		await load();
		poll = setInterval(() => {
			if (step === 'integration' || step === 'connection') refreshIntegration();
		}, 5000);
		unsubs.push(
			...['client_paired', 'client_connected', 'client_disconnected', 'client_kicked', 'client_status_updated', 'resync'].map((type) =>
				onBroadcast(type, refreshClients),
			),
			...['area_created', 'area_deleted', 'areas_deleted', 'resync'].map((type) => onBroadcast(type, refreshAreas)),
		);
	});

	onDestroy(() => {
		clearInterval(poll);
		refreshClients.cancel();
		refreshAreas.cancel();
		unsubs.forEach((fn) => fn());
	});

	async function load() {
		loading = true;
		loadError = '';
		let saved;
		try {
			saved = await api.getSetupAssistant();
		} catch (e) {
			loadError = e.message;
			loading = false;
			return;
		}
		const [i, tk, u, a, c, ps, pl] = await Promise.allSettled([
			api.integrationStatus(),
			api.checkIntegrationToken(),
			api.getHaUrls(),
			api.getAreas(),
			api.getPairedClients(),
			api.getProfileSettings(),
			api.listProfiles(),
		]);
		if (ps.status === 'fulfilled' && pl.status === 'fulfilled') {
			const byId = Object.fromEntries((pl.value?.profiles || []).map((p) => [p.id, p.name]));
			profileDefaults = defaultProfilesSummary(ps.value?.default_profiles, byId);
		}
		integration = i.status === 'fulfilled' ? i.value : null;
		token = tk.status === 'fulfilled' ? tk.value : null;
		if (u.status === 'fulfilled' && u.value) setUrls(u.value);
		areas = a.status === 'fulfilled' && Array.isArray(a.value) ? a.value : [];
		clients = c.status === 'fulfilled' && Array.isArray(c.value) ? c.value : [];

		const skipped = Array.isArray(saved?.skipped) ? saved.skipped.filter((s) => STEPS.includes(s)) : [];
		// Resume where the admin left off; otherwise start at the first open step.
		const resume = saved?.status === 'in_progress' && STEPS.includes(saved.step);
		progress = { status: 'in_progress', step: resume ? saved.step : firstOpenStep(done), skipped };
		loading = false;
		persist();
		if (step === 'area') loadHaAreas();
	}

	function setUrls(r) {
		haUrls = r;
		urlEdit = { ha_url: r.ha_url || '', ha_internal_url: r.ha_internal_url || '' };
	}

	async function refreshIntegration() {
		try {
			const [i, tk] = await Promise.all([api.integrationStatus(), api.checkIntegrationToken()]);
			integration = i;
			token = tk;
		} catch {
			/* keep the last state */
		}
	}

	const refreshClients = debounce(async () => {
		try {
			clients = await api.getPairedClients();
		} catch {
			/* keep the last state */
		}
	}, 250);

	const refreshAreas = debounce(async () => {
		try {
			areas = await api.getAreas();
		} catch {
			/* keep the last state */
		}
	}, 250);

	async function recheck() {
		checking = true;
		await refreshIntegration();
		checking = false;
	}

	async function persist(status = 'in_progress') {
		const body = { status, step: progress.step, skipped: progress.skipped };
		try {
			await api.saveSetupAssistant(body);
			return true;
		} catch (e) {
			addToast(t('setup.saveFailed', { message: e.message }), 'error');
			return false;
		}
	}

	async function go(target) {
		if (!STEPS.includes(target) || target === step) return;
		progress = { ...progress, step: target };
		persist();
		if (target === 'area' && haAreas === null) loadHaAreas();
		if (target === 'integration' || target === 'connection') refreshIntegration();
		await tick();
		heading?.focus();
	}

	function next() {
		go(STEPS[Math.min(index + 1, STEPS.length - 1)]);
	}

	function skip() {
		if (!progress.skipped.includes(step) && !done[step]) progress = { ...progress, skipped: [...progress.skipped, step] };
		next();
	}

	async function later() {
		await persist('dismissed');
		goto(`${base}/`);
	}

	async function finish() {
		if (await persist('completed')) goto(`${base}/`);
	}

	// --- Addresses ---------------------------------------------------------
	async function saveUrls(e) {
		e.preventDefault();
		urlErrors = {
			ha_url: isValidUrl(urlEdit.ha_url) ? '' : t('settings.urlInvalid'),
			ha_internal_url: isValidUrl(urlEdit.ha_internal_url) ? '' : t('settings.urlInvalid'),
		};
		if (urlErrors.ha_url || urlErrors.ha_internal_url) return;
		busy = { ...busy, urls: true };
		try {
			const r = await api.saveHaUrls({ ha_url: urlEdit.ha_url || null, ha_internal_url: urlEdit.ha_internal_url || null });
			if (r && typeof r === 'object') setUrls({ ...haUrls, ...r });
			addToast(t('setup.urls.saved'), 'success');
		} catch (err) {
			addToast(t('settings.haUrlsFailed', { message: err.message }), 'error');
		} finally {
			busy = { ...busy, urls: false };
		}
	}

	// --- First area ----------------------------------------------------------
	async function loadHaAreas() {
		haAreasError = '';
		try {
			const r = await api.getHAAreas();
			haAreas = Array.isArray(r) ? r.toSorted((a, b) => String(a.name).localeCompare(String(b.name))) : [];
		} catch (e) {
			haAreas = [];
			haAreasError = e.message;
		}
	}

	function toggle(id) {
		const n = new Set(selected);
		if (n.has(id)) n.delete(id);
		else n.add(id);
		selected = n;
	}

	async function importAreas() {
		if (!selected.size) return;
		busy = { ...busy, import: true };
		try {
			const r = await api.syncFromHA([...selected], withEntities, null);
			addToast(t('setup.area.importDone', { count: r?.synced ?? selected.size }), 'success');
			const problems = syncProblems(r);
			if (problems) addToast(problems, 'warning', 12000);
			selected = new Set();
			areas = await api.getAreas();
		} catch (e) {
			addToast(t('setup.area.importFailed', { message: e.message }), 'error');
		} finally {
			busy = { ...busy, import: false };
		}
	}

	async function createArea(e) {
		e.preventDefault();
		const name = newName.trim();
		if (!name) return;
		busy = { ...busy, create: true };
		try {
			await api.createArea({ name, is_active: true });
			addToast(t('setup.area.created', { name }), 'success');
			newName = '';
			areas = await api.getAreas();
		} catch (err) {
			addToast(t('setup.area.createFailed', { message: err.message }), 'error');
		} finally {
			busy = { ...busy, create: false };
		}
	}
</script>

<div class="page-narrow wizard" data-testid="setup-assistant">
	<PageHeader title={t('setup.title')} subtitle={t('setup.subtitle')}>
		{#snippet actions()}
			{#if !loading && !loadError}
				<button type="button" class="btn" onclick={later} title={t('setup.laterHint')} data-testid="setup-later">
					<Icon name="clock" />{t('setup.later')}
				</button>
			{/if}
		{/snippet}
	</PageHeader>

	{#if loading}
		<LoadingState />
	{:else if loadError}
		<div class="card"><ErrorState title={t('setup.loadError')} message={loadError} onretry={load} /></div>
	{:else}
		<nav aria-label={t('setup.stepsLabel')}>
			<ol class="steps" data-testid="setup-steps">
				{#each STEPS as s, i (s)}
					{@const st = stateOf(s)}
					<li>
						<button
							type="button"
							class="stepbtn st-{st}"
							class:current={s === step}
							aria-current={s === step ? 'step' : undefined}
							onclick={() => go(s)}
							aria-label="{i + 1}. {t(`setup.steps.${s}`)}{s !== 'done' ? `, ${t(`setup.state.${st}`)}` : ''}"
							data-testid="setup-step-{s}"
							data-state={st}
						>
							<span class="num" aria-hidden="true">
								{#if s !== 'done' && st === 'done'}<Icon name="check" />{:else}{i + 1}{/if}
							</span>
							<span class="lbl">{t(`setup.steps.${s}`)}</span>
						</button>
					</li>
				{/each}
			</ol>
			<div class="progress mt-3" role="progressbar" aria-label={t('setup.stepsLabel')} aria-valuemin="0" aria-valuemax={STEPS.length - 1} aria-valuenow={STEPS.length - 1 - openCount}>
				<i style:width="{((STEPS.length - 1 - openCount) / (STEPS.length - 1)) * 100}%"></i>
			</div>
		</nav>

		<section class="card step mt-4" aria-labelledby="setup-step-title" data-testid="setup-panel-{step}">
			<p class="t-overline">{t('setup.stepOf', { n: index + 1, total: STEPS.length })}</p>
			{#if step === 'integration'}
				<h2 id="setup-step-title" class="t-section" tabindex="-1" bind:this={heading}>{t('setup.integration.title')}</h2>
				<p class="t-sub lead">{t('setup.integration.lead')}</p>
				{#if integration?.connected}
					<Banner tone="ok" testid="setup-integration-ok">
						{integration.integration_version ? t('setup.integration.connectedVersion', { version: integration.integration_version }) : t('setup.integration.connected')}
					</Banner>
				{:else if integration?.last_request_ts}
					<Banner tone="warn" testid="setup-integration-down">{t('setup.integration.down')}</Banner>
				{:else}
					<p>{t('setup.integration.never')}</p>
					<ol class="howto">
						<li>{t('setup.integration.s1')}</li>
						<li>{t('setup.integration.s2')}</li>
						<li>{t('setup.integration.s3')}</li>
						<li>{t('setup.integration.s4')}</li>
					</ol>
				{/if}
				{#if haSupported === false}
					<Banner tone="error" class="mt-3">{t('setup.integration.haTooOld', { version: integration.ha_version, min: MIN_HA_VERSION })}</Banner>
				{/if}
				{#if !integration?.connected}
					<p class="waiting" role="status"><BrandMark size={20} state="loading" />{t('setup.integration.waiting')}</p>
					<div class="eq mt-3">
						<a class="btn" href={myHaIntegrationRepository(BRAND.urls.source.split('/').pop())} target="_blank" rel="noopener noreferrer" data-testid="setup-repo-link">
							<Icon name="external" /><span class="trunc">{t('setup.integration.addRepository')}</span>
						</a>
						<a class="btn" href={myHaAddIntegration(integration?.ha_domain)} target="_blank" rel="noopener noreferrer" data-testid="setup-my-link">
							<Icon name="external" /><span class="trunc">{t('setup.integration.addInHa')}</span>
						</a>
						<a class="btn" href="{BRAND.urls.source}{INSTALL_GUIDE_PATH}" target="_blank" rel="noopener noreferrer">
							<Icon name="file" /><span class="trunc">{t('setup.integration.guide')}</span>
						</a>
						<button type="button" class="btn" onclick={recheck} disabled={checking} data-testid="setup-recheck">
							<Icon name="refresh" /><span class="trunc">{checking ? t('setup.checking') : t('setup.recheck')}</span>
						</button>
					</div>
				{/if}
			{:else if step === 'connection'}
				<h2 id="setup-step-title" class="t-section" tabindex="-1" bind:this={heading}>{t('setup.connection.title')}</h2>
				<p class="t-sub lead">{t('setup.connection.lead')}</p>
				<ul class="checks" data-testid="setup-checks">
					<li>
						<span class="tick" class:ok={token?.has_token && token?.supervisor_managed} class:warn={!token?.has_token || !token?.supervisor_managed}>
							<Icon name={token?.has_token && token?.supervisor_managed ? 'check' : 'warn'} />
						</span>
						<div>
							<b>{t('setup.connection.token')}</b>
							<small>
								{!token?.has_token ? t('setup.connection.tokenMissing') : token?.supervisor_managed ? t('setup.connection.tokenAuto') : t('setup.connection.tokenManual')}
							</small>
						</div>
					</li>
					<li>
						<span class="tick" class:ok={integration?.connected} class:err={!integration?.connected}>
							<Icon name={integration?.connected ? 'check' : 'x'} />
						</span>
						<div>
							<b>{t('setup.connection.link')}</b>
							<small>
								{integration?.connected ? (integration.push_connected ? t('setup.connection.linkPush') : t('setup.connection.linkPoll')) : t('setup.connection.linkDown')}
							</small>
						</div>
					</li>
					<li>
						<span class="tick" class:ok={haSupported === true} class:err={haSupported === false}>
							<Icon name={haSupported === false ? 'x' : haSupported ? 'check' : 'info'} />
						</span>
						<div>
							<b>{t('setup.connection.ha')}</b>
							<small>
								{#if haSupported === false}
									{t('setup.integration.haTooOld', { version: integration.ha_version, min: MIN_HA_VERSION })}
								{:else if integration?.ha_version}
									{t('setup.connection.haOk', { version: integration.ha_version, min: MIN_HA_VERSION })}
								{:else}
									{t('setup.connection.haUnknown')}
								{/if}
							</small>
						</div>
					</li>
					<li>
						<span class="tick ok"><Icon name="users" /></span>
						<div>
							<b>{t('setup.connection.users')}</b>
							<small>{t('setup.connection.usersText')}</small>
						</div>
					</li>
				</ul>
				{#if done.connection}
					<Banner tone="ok" class="mt-3" testid="setup-connection-ok">{t('setup.connection.ok')}</Banner>
				{:else if integration?.auth_failure_count > 0 && !integration?.connected}
					<Banner tone="warn" class="mt-3">{t('setup.connection.staleToken')}</Banner>
				{:else if !integration?.connected}
					<Banner tone="warn" class="mt-3">
						{t('setup.connection.notConnected')}
						{#snippet actions()}<button type="button" class="btn btn-sm" onclick={() => go('integration')}>{t('setup.steps.integration')}</button>{/snippet}
					</Banner>
				{/if}
				{#if token && !token.supervisor_managed}
					<div class="eq mt-3">
						<a class="btn" href="{base}/settings?tab=advanced"><Icon name="key" /><span class="trunc">{t('setup.connection.openAdvanced')}</span></a>
						<button type="button" class="btn" onclick={recheck} disabled={checking}><Icon name="refresh" /><span class="trunc">{checking ? t('setup.checking') : t('setup.recheck')}</span></button>
					</div>
				{/if}
			{:else if step === 'urls'}
				<h2 id="setup-step-title" class="t-section" tabindex="-1" bind:this={heading}>{t('setup.urls.title')}</h2>
				<p class="t-sub lead">{t('setup.urls.lead')}</p>
				<form class="stack" onsubmit={saveUrls} novalidate>
					<div class="grid md:grid-cols-2 gap-4">
						<Field
							label={t('setup.urls.internal')}
							hint={haUrls.ha_internal_url_detected ? t('setup.urls.detected', { url: haUrls.ha_internal_url_detected }) : t('setup.urls.notDetected')}
							error={urlErrors.ha_internal_url}
							id="setup-internal-url"
						>
							{#snippet children({ id, describedby, invalid })}
								<input {id} class="input" type="url" aria-describedby={describedby} aria-invalid={invalid} bind:value={urlEdit.ha_internal_url} placeholder={haUrls.ha_internal_url_detected || 'http://homeassistant.local:8123'} />
							{/snippet}
						</Field>
						<Field
							label={t('setup.urls.external')}
							hint={haUrls.ha_url_detected ? t('setup.urls.detected', { url: haUrls.ha_url_detected }) : t('setup.urls.externalHint')}
							error={urlErrors.ha_url}
							id="setup-external-url"
						>
							{#snippet children({ id, describedby, invalid })}
								<input {id} class="input" type="url" aria-describedby={describedby} aria-invalid={invalid} bind:value={urlEdit.ha_url} placeholder={haUrls.ha_url_detected || 'https://xxx.ui.nabu.casa'} />
							{/snippet}
						</Field>
					</div>
					<div class="eq eq-1">
						<button type="submit" class="btn" disabled={busy.urls} data-testid="setup-save-urls">
							<span class="trunc">{busy.urls ? t('common.saving') : t('setup.urls.save')}</span>
						</button>
					</div>
				</form>
				<div class="box mt-4 remote">
					<Icon name="globe" />
					<div>
						<b>{t('setup.urls.remoteTitle')}</b>
						<p class="t-help">{t('setup.urls.remoteText')}</p>
					</div>
				</div>
				{#if done.urls}<Banner tone="ok" class="mt-3">{t('setup.urls.ok')}</Banner>{/if}
			{:else if step === 'area'}
				<h2 id="setup-step-title" class="t-section" tabindex="-1" bind:this={heading}>{t('setup.area.title')}</h2>
				<p class="t-sub lead">{t('setup.area.lead')}</p>
				{#if done.area}
					<Banner tone="ok" testid="setup-area-ok">
						{t('setup.area.ok', { count: areas.length })}
						{#snippet actions()}<a class="btn btn-sm" href="{base}/areas">{t('setup.area.edit')}</a>{/snippet}
					</Banner>
				{/if}
				<fieldset class="blk">
					<legend class="t-label">{t('setup.area.suggestions')}</legend>
					<p class="t-help mb-2">{t('setup.area.suggestionsHint')}</p>
					{#if haAreas === null}
						<LoadingState inline />
					{:else if haAreasError}
						<Banner tone="warn">{t('setup.area.haFailed', { message: haAreasError })}</Banner>
					{:else if haAreas.length === 0}
						<p class="t-help">{t('setup.area.noSuggestions')}</p>
					{:else}
						<div class="chips" data-testid="setup-ha-areas">
							{#each haAreas as a (a.area_id)}
								{@const already = importedHa.has(a.area_id)}
								<button
									type="button"
									class="chip"
									aria-pressed={selected.has(a.area_id) ? 'true' : 'false'}
									disabled={already}
									onclick={() => toggle(a.area_id)}
									title={already ? t('setup.area.imported') : undefined}
								>
									{#if already || selected.has(a.area_id)}<Icon name="check" />{/if}
									{a.name}{#if already}<span class="sr-only">, {t('setup.area.imported')}</span>{/if}
								</button>
							{/each}
						</div>
						<label class="check mt-3">
							<input type="checkbox" bind:checked={withEntities} />
							<span>{t('setup.area.withEntities')}</span>
						</label>
						<div class="eq eq-1 mt-3">
							<button type="button" class="btn" class:btn-primary={!done.area && selected.size > 0} disabled={!selected.size || busy.import} onclick={importAreas} data-testid="setup-import-areas">
								<Icon name="download" /><span class="trunc">{busy.import ? t('setup.area.importing') : selected.size ? t('setup.area.importN', { count: selected.size }) : t('setup.area.importNone')}</span>
							</button>
						</div>
					{/if}
				</fieldset>
				<form class="blk sep" onsubmit={createArea}>
					<Field label={t('setup.area.own')} id="setup-area-name">
						{#snippet children({ id, describedby })}
							<div class="own">
								<input {id} class="input" aria-describedby={describedby} bind:value={newName} placeholder={t('setup.area.namePlaceholder')} maxlength="100" />
								<button type="submit" class="btn" disabled={!newName.trim() || busy.create} data-testid="setup-create-area">
									<Icon name="plus" />{t('setup.area.create')}
								</button>
							</div>
						{/snippet}
					</Field>
				</form>
			{:else if step === 'device'}
				<h2 id="setup-step-title" class="t-section" tabindex="-1" bind:this={heading}>{t('setup.device.title')}</h2>
				<p class="t-sub lead">{t('setup.device.lead')}</p>
				{#if profileDefaults}
					<div class="box mb-3 profdef" data-testid="setup-profiles">
						<Icon name="template" />
						<p class="min-w-0">
							<b>{t('setup.device.profiles')}</b>
							{profileDefaults}.
							<a class="link" href="{base}/profiles">{t('setup.device.profilesLink')}</a>
						</p>
					</div>
				{/if}
				<ol class="cards3">
					<li class="box tile">
						<span class="tnum" aria-hidden="true">1</span>
						<b>{t('setup.device.s1')}</b>
						<p class="t-help">{t('setup.device.s1Text')}</p>
						{#if !isPlaceholderUrl(APP_STORE_URL)}
							<a class="btn btn-block" href={APP_STORE_URL} target="_blank" rel="noopener noreferrer" data-testid="setup-app-store"><Icon name="download" /><span class="trunc">{t('setup.device.appStore')}</span></a>
						{/if}
					</li>
					<li class="box tile">
						<span class="tnum" aria-hidden="true">2</span>
						<b>{t('setup.device.s2')}</b>
						<p class="t-help">{t('setup.device.s2Text')}</p>
						<button type="button" class="btn btn-block" class:btn-primary={!done.device} onclick={() => openPairing('member', { stay: true })} data-testid="setup-pair">
							<Icon name="qr" /><span class="trunc">{t('setup.device.pair')}</span>
						</button>
					</li>
					<li class="box tile">
						<span class="tnum" aria-hidden="true">3</span>
						<b>{t('setup.device.s3')}</b>
						<p class="t-help">{t('setup.device.s3Text')}</p>
						{#if members.length === 0}
							<p class="waiting" role="status"><BrandMark size={20} state="loading" />{t('setup.device.waiting')}</p>
						{:else}
							<ul class="devs" data-testid="setup-devices">
								{#each members.slice(0, 3) as c (c.id)}
									<li>
										<span class="trunc flex-1">{c.name}</span>
										{#if c.ha_token_status === 'pending'}<small class="t-help">{t('setup.device.settingUp')}</small>{:else}<StatusBadge online={c.is_connected} />{/if}
									</li>
								{/each}
							</ul>
						{/if}
					</li>
				</ol>
				{#if done.device}<Banner tone="ok" class="mt-3" testid="setup-device-ok">{t('setup.device.ok', { count: members.length })}</Banner>{/if}
			{:else}
				<h2 id="setup-step-title" class="t-section" tabindex="-1" bind:this={heading}>{openCount ? t('setup.done.titleOpen') : t('setup.done.title')}</h2>
				<p class="t-sub lead">{openCount ? t('setup.done.leadOpen') : t('setup.done.lead')}</p>
				<h3 class="t-label">{t('setup.done.summary')}</h3>
				<ul class="list summary" data-testid="setup-summary">
					{#each STEPS.slice(0, -1) as s (s)}
						{@const st = stateOf(s)}
						<li data-state={st}>
							<span class="tick" class:ok={st === 'done'} class:warn={st === 'skipped'}>
								<Icon name={st === 'done' ? 'check' : st === 'skipped' ? 'arrowdown' : 'minus'} />
							</span>
							<span class="flex-1">{t(`setup.steps.${s}`)}</span>
							<span class="badge" class:badge-ok={st === 'done'} class:badge-warn={st === 'skipped'}>{t(`setup.state.${st}`)}</span>
							{#if st !== 'done'}
								<button type="button" class="btn btn-sm btn-icon" onclick={() => go(s)} aria-label={t('setup.done.goTo', { step: t(`setup.steps.${s}`) })}>
									<Icon name="chev" />
								</button>
							{:else}
								<span class="slot" aria-hidden="true"></span>
							{/if}
						</li>
					{/each}
				</ul>
				<h3 class="t-label mt-4">{t('setup.done.next')}</h3>
				<ul class="nextgrid" data-testid="setup-next">
					{#each NEXT as n (n.key)}
						<li>
							<a class="box nextcard" href="{base}{n.href}">
								<span class="nhead">
									<Icon name={n.icon} />
									<b class="trunc">{t(`setup.done.${n.key}`)}</b>
									{#if n.pro}<span class="badge badge-accent">{t('setup.done.pro')}</span>{/if}
								</span>
								<span class="t-help clamp3">{t(`setup.done.${n.key}Text`)}</span>
							</a>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<div class="wiznav" class:two={step === 'done'} data-testid="setup-nav">
			<button type="button" class="btn" onclick={() => go(STEPS[index - 1])} disabled={index === 0} data-testid="setup-back">
				<Icon name="back" /><span class="trunc">{t('setup.back')}</span>
			</button>
			{#if step !== 'done'}
				<button type="button" class="btn" onclick={skip} disabled={done[step]} data-testid="setup-skip">
					<span class="trunc">{t('setup.skip')}</span>
				</button>
				<button type="button" class="btn btn-primary" onclick={next} data-testid="setup-next-btn">
					<span class="trunc">{t('setup.next')}</span><Icon name="chev" />
				</button>
			{:else}
				<button type="button" class="btn btn-primary" onclick={finish} data-testid="setup-finish">
					<Icon name="check" /><span class="trunc">{t('setup.finish')}</span>
				</button>
			{/if}
		</div>
	{/if}
</div>

<style>
	.profdef {
		display: flex;
		gap: 10px;
		align-items: flex-start;
	}
	.profdef p {
		margin: 0;
	}
	.wizard {
		padding-bottom: 8px;
	}
	.steps {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(6, minmax(0, 1fr));
		gap: 8px;
	}
	.stepbtn {
		width: 100%;
		height: 100%;
		min-height: 76px;
		display: grid;
		justify-items: center;
		align-content: start;
		gap: 6px;
		padding: 10px 6px;
		border-radius: var(--r-box);
		border: 1px solid var(--line);
		background: var(--surface);
		color: var(--ink-2);
		font: inherit;
		font-size: var(--fs-sm);
		font-weight: 600;
		text-align: center;
		cursor: pointer;
	}
	.stepbtn:hover {
		background: var(--surface-2);
	}
	.stepbtn.current {
		border-color: var(--ink);
		box-shadow: 0 0 0 1px var(--ink);
		color: var(--ink);
	}
	.num {
		width: 28px;
		height: 28px;
		border-radius: 14px;
		display: grid;
		place-items: center;
		border: 2px solid var(--line-2);
		box-sizing: border-box;
		font-size: 13px;
		font-weight: 700;
	}
	.num :global(.ic) {
		width: 16px;
		height: 16px;
	}
	.st-done .num {
		background: var(--ok);
		border-color: var(--ok);
		color: #fff;
	}
	.st-skipped .num {
		border-style: dashed;
	}
	.current .num {
		border-color: var(--ink);
	}
	.lbl {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: 2.5em;
		line-height: 1.25;
		overflow-wrap: anywhere;
	}
	.step {
		padding: var(--s5);
	}
	.step h2 {
		margin: 2px 0 6px;
	}
	.step h2:focus {
		outline: none;
	}
	.lead {
		margin: 0 0 var(--s4);
	}
	.howto {
		list-style: decimal;
		margin: 8px 0 0;
		padding-left: 22px;
		display: grid;
		gap: 8px;
	}
	.waiting {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 12px 0 0;
		color: var(--ink-2);
		font-size: var(--fs-sm);
	}
	/* Equal sizes: buttons of one row share the width, text is cut, never the button. */
	.eq {
		display: grid;
		grid-auto-flow: column;
		grid-auto-columns: minmax(0, 1fr);
		gap: 8px;
	}
	.eq-1 {
		grid-auto-columns: minmax(0, 260px);
	}
	.trunc {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.checks {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 10px;
	}
	.checks li,
	.remote {
		display: flex;
		align-items: flex-start;
		gap: 12px;
	}
	.checks b,
	.remote b {
		display: block;
		font-weight: 650;
	}
	.checks small {
		display: block;
		color: var(--ink-2);
		font-size: var(--fs-sm);
	}
	.remote > :global(.ic) {
		width: 20px;
		height: 20px;
		flex: none;
		color: var(--ink-2);
		margin-top: 2px;
	}
	.remote p {
		margin: 4px 0 0;
	}
	.tick {
		width: 26px;
		height: 26px;
		border-radius: 13px;
		display: grid;
		place-items: center;
		flex: none;
		background: var(--fill-2);
		color: var(--ink-2);
	}
	.tick :global(.ic) {
		width: 15px;
		height: 15px;
	}
	.tick.ok {
		background: var(--ok-soft);
		color: var(--ok-ink);
	}
	.tick.warn {
		background: var(--warn-soft);
		color: var(--warn-ink);
	}
	.tick.err {
		background: var(--err-soft);
		color: var(--err-ink);
	}
	.blk {
		border: 0;
		margin: 16px 0 0;
		padding: 0;
		min-width: 0;
	}
	.blk.sep {
		margin-top: 20px;
		padding-top: 16px;
		border-top: 1px solid var(--line);
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.chip:disabled {
		opacity: 0.6;
	}
	.own {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 140px;
		gap: 8px;
	}
	.cards3 {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		grid-auto-rows: 1fr;
		gap: 12px;
	}
	.tile {
		display: grid;
		grid-template-rows: auto auto 1fr auto;
		gap: 6px;
		padding: var(--s4);
	}
	.tile p {
		margin: 0;
	}
	.tnum {
		width: 26px;
		height: 26px;
		border-radius: 13px;
		display: grid;
		place-items: center;
		background: var(--ink);
		color: var(--surface);
		font-weight: 700;
		font-size: 13px;
	}
	.devs {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 6px;
	}
	.devs li {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
	.slot {
		width: var(--h-ctl-sm);
		height: var(--h-ctl-sm);
		flex: none;
	}
	.summary .tick {
		width: 24px;
		height: 24px;
	}
	.nextgrid {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		grid-auto-rows: 1fr;
		gap: 10px;
	}
	.nextgrid li {
		display: flex;
	}
	.nextcard {
		flex: 1;
		display: grid;
		grid-template-rows: auto 1fr;
		gap: 6px;
		color: inherit;
		text-decoration: none;
		min-height: 108px;
		box-sizing: border-box;
	}
	.nextcard:hover {
		background: var(--fill-2);
	}
	.nhead {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
	.nhead :global(.ic) {
		width: 18px;
		height: 18px;
		flex: none;
	}
	.clamp3 {
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: 3.9em;
	}
	.wiznav {
		position: sticky;
		bottom: 0;
		z-index: 5;
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 8px;
		margin-top: var(--s4);
		padding: 10px 0;
		background: var(--bg);
	}
	.wiznav.two {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}
	@media (max-width: 767px) {
		.cards3 {
			grid-template-columns: minmax(0, 1fr);
		}
		.step {
			padding: var(--s4);
		}
	}
	@media (max-width: 639px) {
		.steps {
			gap: 4px;
		}
		.stepbtn {
			min-height: 48px;
			padding: 8px 2px;
		}
		.lbl {
			position: absolute;
			width: 1px;
			height: 1px;
			overflow: hidden;
			clip: rect(0, 0, 0, 0);
			min-height: 0;
		}
		.nextgrid {
			grid-template-columns: minmax(0, 1fr);
		}
		.eq {
			grid-auto-flow: row;
		}
		.wiznav {
			gap: 6px;
		}
		.wiznav .btn {
			padding: 0 6px;
			gap: 4px;
		}
		.eq-1 {
			grid-auto-columns: minmax(0, 1fr);
		}
		.own {
			grid-template-columns: minmax(0, 1fr) 110px;
		}
	}
</style>
