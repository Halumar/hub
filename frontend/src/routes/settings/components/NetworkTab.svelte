<script>
	/** Network: Home Assistant addresses, hub address and further networks that count as at home. */
	import { api } from '$lib/api.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import LoadError from '$lib/components/LoadError.svelte';
	import HaCleartextHint from '$lib/components/HaCleartextHint.svelte';
	import { settings as S, isValidUrl } from '../settings-state.svelte.js';
	import { MAX_HOME_NETWORKS, TEMPLATES, inSharedRange, looksLikeCidr } from './home-networks.js';

	let haUrlError = $state('');
	let haInternalUrlError = $state('');
	let appUrlError = $state('');
	let saving = $state({});

	function busy(key, v) {
		saving = { ...saving, [key]: v };
	}

	async function saveHaUrls(e) {
		e.preventDefault();
		haUrlError = isValidUrl(S.haUrlEdit.ha_url) ? '' : t('settings.urlInvalid');
		haInternalUrlError = isValidUrl(S.haUrlEdit.ha_internal_url) ? '' : t('settings.urlInvalid');
		if (haUrlError || haInternalUrlError) return;
		busy('ha', true);
		const r = await S.save(
			() => api.saveHaUrls({ ha_url: S.haUrlEdit.ha_url || null, ha_internal_url: S.haUrlEdit.ha_internal_url || null }),
			t('settings.haUrlsSaved'),
			'settings.haUrlsFailed',
		);
		if (r && typeof r === 'object') {
			S.haUrls = r;
			S.haUrlEdit = { ha_url: r.ha_url || '', ha_internal_url: r.ha_internal_url || '' };
		}
		busy('ha', false);
	}

	function resetHaUrls() {
		S.haUrlEdit = { ha_url: S.haUrls.ha_url_detected || '', ha_internal_url: S.haUrls.ha_internal_url_detected || '' };
	}

	async function saveAppUrl(e) {
		e?.preventDefault();
		appUrlError = isValidUrl(S.appUrlEdit) ? '' : t('settings.urlInvalid');
		if (appUrlError) return;
		busy('app', true);
		const r = await S.save(() => api.saveAppUrl(S.appUrlEdit || ''), null, 'settings.appUrlFailed');
		if (r) {
			S.appUrl = r.app_url || '';
			S.appUrlEdit = S.appUrl;
			addToast(S.appUrl ? t('settings.appUrlSaved') : t('settings.appUrlCleared'), 'success');
		}
		busy('app', false);
	}

	// ── Further home networks ─────────────────────────────────────
	let template = $state(null);
	let cidr = $state('');
	let name = $state('');
	let withIpv6 = $state(true);
	let netError = $state('');
	let full = $derived(S.homeNetworks.length >= MAX_HOME_NETWORKS);
	let sharedRange = $derived(S.homeNetworks.some((n) => inSharedRange(n.cidr)));

	function pick(id) {
		template = id;
		netError = '';
		const tpl = TEMPLATES[id];
		name = tpl.name || '';
		cidr = tpl.fixed || '';
		withIpv6 = true;
	}

	function errorText(e) {
		if (['home_network_invalid', 'home_network_public', 'home_networks_too_many'].includes(e?.code)) {
			return t(`settings.homeNetworks.errors.${e.code}`, { cidr: e.cidr || cidr });
		}
		return t('settings.homeNetworks.failed', { message: e?.message || '' });
	}

	async function store(networks) {
		busy('nets', true);
		netError = '';
		try {
			const r = await api.saveHomeNetworks(networks);
			S.homeNetworks = Array.isArray(r?.networks) ? r.networks : networks;
			addToast(t('settings.homeNetworks.saved'), 'success');
			return true;
		} catch (e) {
			netError = errorText(e);
			return false;
		} finally {
			busy('nets', false);
		}
	}

	async function add(e) {
		e.preventDefault();
		const value = cidr.trim();
		if (!looksLikeCidr(value)) {
			netError = t('settings.homeNetworks.errors.home_network_invalid', { cidr: value || '–' });
			return;
		}
		const extra = [{ cidr: value, name: name.trim() || null }];
		const tpl = template && TEMPLATES[template];
		if (tpl?.ipv6 && withIpv6) extra.push({ cidr: tpl.ipv6, name: name.trim() || null });
		if (S.homeNetworks.length + extra.length > MAX_HOME_NETWORKS) {
			netError = t('settings.homeNetworks.errors.home_networks_too_many');
			return;
		}
		if (await store([...S.homeNetworks, ...extra])) {
			template = null;
			cidr = '';
			name = '';
		}
	}

	async function remove(entry) {
		await store(S.homeNetworks.filter((n) => n.cidr !== entry.cidr));
	}
</script>

<section class="card" aria-labelledby="ha-h">
	<div class="card-head"><Icon name="home" /><h2 id="ha-h">{t('settings.haUrls')}</h2></div>
	<p class="t-help mb-3">{t('settings.haUrlsHint')}</p>
	<HaCleartextHint haUrls={S.haUrls} class="mb-3" />
	{#if S.loading.haUrls && !S.haUrls.ha_url_detected}
		<LoadingState inline />
	{:else if S.loadErrors.haUrls}
		<LoadError message={S.loadErrors.haUrls} onretry={S.loaders.haUrls} />
	{:else}
		<form class="stack" onsubmit={saveHaUrls}>
			<div class="grid md:grid-cols-2 gap-4">
				<Field label={t('settings.externalUrl')} hint={S.haUrls.ha_url_detected ? t('settings.detected', { url: S.haUrls.ha_url_detected }) : ''} error={haUrlError} id="ha-url">
					{#snippet children({ id, describedby, invalid })}
						<input {id} class="input" type="url" aria-describedby={describedby} aria-invalid={invalid} bind:value={S.haUrlEdit.ha_url} placeholder="https://xxx.ui.nabu.casa" />
					{/snippet}
				</Field>
				<Field label={t('settings.internalUrl')} hint={S.haUrls.ha_internal_url_detected ? t('settings.detected', { url: S.haUrls.ha_internal_url_detected }) : ''} error={haInternalUrlError} id="ha-internal-url">
					{#snippet children({ id, describedby, invalid })}
						<input {id} class="input" type="url" aria-describedby={describedby} aria-invalid={invalid} bind:value={S.haUrlEdit.ha_internal_url} placeholder="http://198.51.100.178:8123" />
					{/snippet}
				</Field>
			</div>
			<div class="row">
				<button type="submit" class="btn btn-primary" disabled={saving.ha}>{saving.ha ? t('common.saving') : t('common.save')}</button>
				{#if S.haUrls.ha_url_detected || S.haUrls.ha_internal_url_detected}
					<button type="button" class="btn" onclick={resetHaUrls}>{t('settings.resetDetected')}</button>
				{/if}
			</div>
		</form>
	{/if}
</section>

<section class="card" aria-labelledby="app-h">
	<div class="card-head"><Icon name="link" /><h2 id="app-h">{t('settings.appUrl')}</h2></div>
	<p class="t-help mb-3">{t('settings.appUrlHint')}</p>
	{#if S.loadErrors.appUrl}
		<LoadError message={S.loadErrors.appUrl} onretry={S.loaders.appUrl} />
	{:else}
		<form class="stack" onsubmit={saveAppUrl}>
			<Field label={t('settings.appUrlLabel')} error={appUrlError} id="app-url">
				{#snippet aside()}{#if !S.appUrl}<span class="badge">{t('settings.autoDetect')}</span>{/if}{/snippet}
				{#snippet children({ id, describedby, invalid })}
					<input {id} class="input" type="url" aria-describedby={describedby} aria-invalid={invalid} bind:value={S.appUrlEdit} placeholder="https://hub.example.com" />
				{/snippet}
			</Field>
			<div class="row">
				<button type="submit" class="btn btn-primary" disabled={saving.app}>{saving.app ? t('common.saving') : t('common.save')}</button>
				{#if S.appUrl}
					<button type="button" class="btn" onclick={() => { S.appUrlEdit = ''; saveAppUrl(); }}>{t('settings.clear')}</button>
				{/if}
			</div>
		</form>
	{/if}
</section>

<section class="card" aria-labelledby="nets-h" data-testid="home-networks">
	<div class="card-head"><Icon name="wifi" /><h2 id="nets-h">{t('settings.homeNetworks.title')}</h2></div>
	<p class="t-help mb-3">{t('settings.homeNetworks.hint')}</p>
	{#if S.loading.homeNetworks && S.homeNetworks.length === 0}
		<LoadingState inline />
	{:else if S.loadErrors.homeNetworks}
		<LoadError message={S.loadErrors.homeNetworks} onretry={S.loaders.homeNetworks} />
	{:else}
		<div class="stack">
			{#if sharedRange}
				<Banner tone="warn" live={false} testid="home-networks-shared">{t('settings.homeNetworks.sharedRange')}</Banner>
			{/if}
			{#if S.homeNetworks.length === 0}
				<p class="t-help" data-testid="home-networks-empty">{t('settings.homeNetworks.empty')}</p>
			{:else}
				<ul class="nets" aria-label={t('settings.homeNetworks.title')}>
					{#each S.homeNetworks as n (n.cidr)}
						<li class="net" data-testid="home-network">
							<span class="net-text"><code>{n.cidr}</code>{#if n.name}<span class="t-help">{n.name}</span>{/if}</span>
							<button type="button" class="btn btn-ghost btn-sm" disabled={saving.nets} onclick={() => remove(n)} aria-label={t('settings.homeNetworks.remove', { cidr: n.cidr })}>
								<Icon name="trash" />
							</button>
						</li>
					{/each}
				</ul>
			{/if}

			<div class="templates" role="group" aria-label={t('settings.homeNetworks.templates')}>
				{#each Object.keys(TEMPLATES) as id (id)}
					<button type="button" class="btn tpl" aria-pressed={template === id} disabled={full} onclick={() => pick(id)} data-testid="home-network-template-{id}">
						{t(`settings.homeNetworks.template.${id}`)}
					</button>
				{/each}
			</div>

			{#if template === 'tailscale' && !sharedRange}
				<Banner tone="warn" live={false} testid="home-networks-tailscale">{t('settings.homeNetworks.sharedRange')}</Banner>
			{/if}

			<form class="stack" onsubmit={add}>
				<div class="grid md:grid-cols-2 gap-4">
					<Field label={t('settings.homeNetworks.cidr')} hint={t('settings.homeNetworks.cidrHint')} id="home-network-cidr">
						{#snippet children({ id, describedby, invalid })}
							<input {id} class="input mono" aria-describedby={describedby} aria-invalid={invalid} bind:value={cidr} readonly={!!TEMPLATES[template]?.fixed}
								placeholder={TEMPLATES[template]?.example || '10.8.0.0/24'} autocomplete="off" spellcheck="false" disabled={full} />
						{/snippet}
					</Field>
					<Field label={t('settings.homeNetworks.name')} id="home-network-name">
						{#snippet children({ id, describedby })}
							<input {id} class="input" aria-describedby={describedby} bind:value={name} maxlength="40" placeholder={t('settings.homeNetworks.namePlaceholder')} disabled={full} />
						{/snippet}
					</Field>
				</div>
				{#if TEMPLATES[template]?.ipv6}
					<label class="row check"><input type="checkbox" bind:checked={withIpv6} data-testid="home-network-ipv6" />{t('settings.homeNetworks.ipv6', { cidr: TEMPLATES[template].ipv6 })}</label>
				{/if}
				{#if netError}<p class="field-error" role="alert" data-testid="home-networks-error">{netError}</p>{/if}
				<div class="row">
					<button type="submit" class="btn btn-primary" disabled={saving.nets || full} data-testid="home-network-add">
						<Icon name="plus" />{t('settings.homeNetworks.add')}
					</button>
					<span class="t-help">{t('settings.homeNetworks.count', { count: S.homeNetworks.length, max: MAX_HOME_NETWORKS })}</span>
				</div>
			</form>
		</div>
	{/if}
</section>

<style>
	.nets {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 0.5rem;
	}
	.net {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.5rem 0.75rem;
		border: 1px solid var(--border, rgba(0, 0, 0, 0.1));
		border-radius: 0.5rem;
	}
	.net-text {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.75rem;
		min-width: 0;
	}
	.templates {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 0.5rem;
	}
	@media (min-width: 640px) {
		.templates {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
	}
	.tpl {
		justify-content: center;
		width: 100%;
	}
	.tpl[aria-pressed='true'] {
		outline: 2px solid var(--accent, currentColor);
		outline-offset: -2px;
	}
	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.check {
		gap: 0.5rem;
		align-items: center;
	}
</style>
