<script>
	/** General: this admin UI (language, theme), home screen of the apps, control mode, API endpoints. */
	import { api, resolveImageSrc } from '$lib/api.js';
	import { t, i18n } from '$lib/i18n/index.svelte.js';
	import { theme } from '$lib/theme.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';
	import LoadError from '$lib/components/LoadError.svelte';
	import ScanTagsCard from './ScanTagsCard.svelte';
	import { settings as S } from '../settings-state.svelte.js';

	let saving = $state({});
	let uploading = $state({});
	// API docs row only when the add-on serves them (option api_docs).
	let docsAvailable = $state(false);
	$effect(() => {
		api.apiDocsAvailable().then((ok) => (docsAvailable = ok));
	});

	const MODES = ['basic', 'advanced', 'full'];

	function busy(key, v) {
		saving = { ...saving, [key]: v };
	}

	async function saveHomeTitle(e) {
		e.preventDefault();
		busy('home', true);
		const r = await S.save(() => api.saveHomeSettings({ home_title: S.homeTitleEdit || '' }), t('settings.homeTitleSaved'), 'settings.homeTitleFailed');
		if (r && typeof r === 'object') S.home = r;
		busy('home', false);
	}

	async function uploadBg(type, event) {
		const input = event.currentTarget;
		const file = input.files?.[0];
		if (!file) return;
		uploading = { ...uploading, [type]: true };
		const ok = await S.save(() => (type === 'bg_light' ? api.uploadHomeBgLight(file) : api.uploadHomeBgDark(file)), t('settings.bgUploaded'), 'settings.bgFailed');
		if (ok) await S.loaders.home();
		uploading = { ...uploading, [type]: false };
		input.value = '';
	}

	async function deleteBg(type) {
		const ok = await S.save(() => (type === 'bg_light' ? api.deleteHomeBgLight() : api.deleteHomeBgDark()), t('settings.bgRemoved'), 'settings.bgFailed');
		if (ok) await S.loaders.home();
	}

	async function setControlMode(mode) {
		const prev = S.controlMode;
		S.controlMode = mode;
		busy('mode', true);
		const r = await S.save(() => api.saveDefaultControlMode({ control_mode: mode }), t('settings.controlModeSaved'), 'settings.controlModeFailed');
		if (r && typeof r === 'object') S.controlMode = r.control_mode || mode;
		else if (!r) S.controlMode = prev;
		busy('mode', false);
	}

	// Best-effort hub base URL for the API hints (display only; pairing uses the server-built URL).
	let hubUrl = $derived.by(() => {
		if (S.appUrl) return S.appUrl;
		if (typeof window === 'undefined') return '';
		const host = window.location.hostname;
		if (window.__INGRESS_PATH) {
			const internal = S.haUrls.ha_internal_url || S.haUrls.ha_internal_url_detected || '';
			try {
				if (internal) return `https://${new URL(internal).hostname}:8100`;
			} catch {
				/* fall through */
			}
			return `https://${host}:8100`;
		}
		return window.location.origin;
	});

	const BGS = [
		{ type: 'bg_light', field: 'home_bg_light', key: 'bgLight' },
		{ type: 'bg_dark', field: 'home_bg_dark', key: 'bgDark' },
	];
</script>

<section class="card" aria-labelledby="ui-h">
	<div class="card-head"><Icon name="palette" /><h2 id="ui-h">{t('settings.adminUi')}</h2></div>
	<p class="t-help mb-3">{t('settings.adminUiHint')}</p>
	<div class="grid sm:grid-cols-2 gap-4">
		<div class="field">
			<span class="field-label" id="lang-label">{t('settings.language')}</span>
			<Segmented
				label={t('settings.language')}
				options={['auto', 'en', 'de'].map((v) => ({ value: v, label: t(`settings.languages.${v}`) }))}
				value={i18n.preference}
				onchange={(v) => i18n.setPreference(v)}
				testid="language-switch"
			/>
			<p class="t-help">{t('settings.languageHint')}</p>
		</div>
		<div class="field">
			<span class="field-label">{t('settings.theme')}</span>
			<Segmented
				label={t('settings.theme')}
				options={['auto', 'light', 'dark'].map((v) => ({ value: v, label: t(`settings.themes.${v}`) }))}
				value={theme.preference}
				onchange={(v) => theme.setPreference(v)}
				testid="theme-switch"
			/>
			<p class="t-help">{t('settings.themeHint')}</p>
		</div>
	</div>
</section>

<section class="card" aria-labelledby="home-h">
	<div class="card-head"><Icon name="iphone" /><h2 id="home-h">{t('settings.home')}</h2></div>
	<p class="t-help mb-3">{t('settings.homeHint')}</p>
	{#if S.loadErrors.home}
		<LoadError message={S.loadErrors.home} onretry={S.loaders.home} />
	{:else}
		<div class="stack">
			<form onsubmit={saveHomeTitle}>
				<Field label={t('settings.homeTitle')} hint={S.home.location_name ? t('settings.homeTitleHa', { name: S.home.location_name }) : ''} id="home-title">
					{#snippet children({ id, describedby })}
						<div class="row flex-nowrap">
							<input {id} class="input" aria-describedby={describedby} bind:value={S.homeTitleEdit} placeholder={S.home.location_name || 'Home'} />
							<button type="submit" class="btn btn-primary" disabled={saving.home}>{saving.home ? t('common.saving') : t('common.save')}</button>
						</div>
					{/snippet}
				</Field>
			</form>
			<div class="grid sm:grid-cols-2 gap-4">
				{#each BGS as bg (bg.type)}
					{@const name = t(`settings.${bg.key}`)}
					<div class="field">
						<span class="field-label">{name}</span>
						{#if S.home[bg.field]}
							<div class="bg-prev">
								<img src={resolveImageSrc(S.home[bg.field])} alt={t('settings.bgAlt', { name })} />
								<button type="button" class="btn btn-sm bg-remove" onclick={() => deleteBg(bg.type)} aria-label={t('settings.removeBg', { name })}><Icon name="trash" /></button>
							</div>
						{/if}
						<label class="btn btn-sm file-btn self-start">
							<Icon name="upload" />{uploading[bg.type] ? t('settings.uploading') : S.home[bg.field] ? t('settings.replace') : t('settings.upload')}
							<input type="file" class="sr-only" accept="image/*" onchange={(e) => uploadBg(bg.type, e)} disabled={uploading[bg.type]} />
						</label>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</section>

<section class="card" aria-labelledby="mode-h">
	<div class="card-head"><Icon name="sparkle" /><h2 id="mode-h">{t('settings.controlMode')}</h2></div>
	<p class="t-help mb-3">{t('settings.controlModeHint')}</p>
	{#if S.loadErrors.controlMode}
		<LoadError message={S.loadErrors.controlMode} onretry={S.loaders.controlMode} />
	{:else}
		<Segmented label={t('settings.controlMode')} options={MODES.map((m) => ({ value: m, label: t(`areas.modes.${m}`) }))} value={S.controlMode} disabled={saving.mode} onchange={setControlMode} />
		<dl class="dl box mt-3">
			{#each MODES as m (m)}
				<dt>{t(`areas.modes.${m}`)}</dt>
				<dd>{t(`settings.exampleModes.${m}`)}</dd>
			{/each}
		</dl>
		<p class="t-help mt-1">{t('settings.example')}</p>
	{/if}
</section>

<ScanTagsCard />

<section class="card" aria-labelledby="api-h">
	<div class="card-head"><Icon name="terminal" /><h2 id="api-h">{t('settings.api')}</h2></div>
	<p class="t-help mb-3">{t('settings.apiHint')}</p>
	<ul class="list">
		{#if docsAvailable}
			<li>
				<span class="badge">REST</span>
				<code class="t-mono flex-1 min-w-0 break-all">{hubUrl}/api/docs</code>
				<!-- Opened from here: this admin UI's own path (Ingress), the docs are admin-only. -->
				<a class="link" href={api.apiDocsUrl('docs')} target="_blank" rel="noopener noreferrer">Swagger</a>
				<a class="link" href={api.apiDocsUrl('redoc')} target="_blank" rel="noopener noreferrer">ReDoc</a>
			</li>
		{/if}
		<li>
			<span class="badge">WS</span>
			<code class="t-mono flex-1 min-w-0 break-all">{hubUrl.replace(/^http/, 'ws')}/api/ws</code>
			<span class="t-help">{t('settings.wsAuth', { example: '{"type":"auth",…}' })}</span>
		</li>
	</ul>
</section>

<style>
	.bg-prev {
		position: relative;
		border-radius: var(--r-box);
		overflow: hidden;
		border: 1px solid var(--line);
	}
	.bg-prev img {
		display: block;
		width: 100%;
		height: 96px;
		object-fit: cover;
	}
	.bg-remove {
		position: absolute;
		top: 6px;
		right: 6px;
	}
</style>
