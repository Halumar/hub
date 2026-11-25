<script>
	/**
	 * Profiles: every device has exactly one profile. List of the
	 * profiles (equal cards), "New profile", default profiles per device type
	 * and the gallery of suggestions (copies; new versions only on request).
	 */
	import { onMount, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { debounce } from '$lib/utils.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Modal from '$lib/ui/Modal.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import ProfileCard from '$lib/components/profiles/ProfileCard.svelte';
	import ProfileIcon from '$lib/components/profiles/ProfileIcon.svelte';
	import DefaultProfilesCard from '$lib/components/profiles/DefaultProfilesCard.svelte';
	import NewProfileDialog from '$lib/components/profiles/NewProfileDialog.svelte';
	import { loadCatalog, L, ruleOf } from '$lib/profiles/catalog.svelte.js';
	import { formatValue, textOf } from '$lib/profiles/format.js';
	import { areaNamer } from '$lib/profiles/context.js';
	import { errorText } from '$lib/profiles/errors.js';

	let profiles = $state([]);
	let maxProfiles = $state(50);
	let suggestions = $state([]);
	let clients = $state([]);
	let areas = $state([]);
	let loading = $state(true);
	let loadError = $state('');

	let filter = $state('all');
	let newOpen = $state(false);
	let newMode = $state('choose');
	let previewOf = $state(null);
	let previewOpen = $state(false);
	let adding = $state('');
	let gallery = $state();

	let ctx = $derived({ areaName: areaNamer(areas) });

	onMount(() => {
		load();
		const refresh = debounce(load, 300);
		const offs = ['client_paired', 'client_kicked', 'client_config_updated', 'profiles_changed', 'resync'].map((type) => onBroadcast(type, refresh));
		return () => {
			offs.forEach((off) => off());
			refresh.cancel();
		};
	});

	async function load() {
		try {
			const [p, s] = await Promise.all([api.listProfiles(), api.getProfileSuggestions(), loadCatalog()]);
			profiles = p?.profiles || [];
			maxProfiles = p?.max_profiles || 50;
			suggestions = s?.suggestions || [];
			loadError = '';
		} catch (e) {
			loadError = e.message;
		} finally {
			loading = false;
		}
		const [a, c] = await Promise.allSettled([api.getAreas(), api.getPairedClients()]);
		if (a.status === 'fulfilled' && Array.isArray(a.value)) areas = a.value;
		if (c.status === 'fulfilled' && Array.isArray(c.value)) clients = c.value.filter((x) => !x.pending_deletion);
	}

	const FILTERS = ['all', 'people', 'kids', 'guests', 'shared'];
	let shown = $derived(filter === 'all' ? suggestions : suggestions.filter((s) => s.group === filter));
	let atLimit = $derived(profiles.length >= maxProfiles);

	function updateFor(s) {
		return profiles.find((p) => s.profile_ids?.includes(p.id) && p.suggestion_update) || null;
	}

	function badgesOf(s) {
		const b = [];
		const upd = updateFor(s);
		if (upd) b.push({ id: 'update', tone: 'badge-info', icon: 'sparkle', text: t('profiles.gallery.newVersion', { version: upd.suggestion_update.version }) });
		if (s.child_protection) b.push({ id: 'child', tone: 'badge-accent', icon: 'child', text: t('profiles.childProtection') });
		if (s.profile_ids?.length) b.push({ id: 'exists', tone: 'badge-ok', icon: 'check', text: t('profiles.gallery.exists') });
		else if (s.starter) b.push({ id: 'starter', tone: '', icon: 'sparkle', text: t('profiles.gallery.starter') });
		return b;
	}

	async function add(s) {
		adding = s.id;
		try {
			const p = await api.addProfileSuggestion(s.id);
			addToast(t('profiles.gallery.added', { name: p.name }), 'success');
			previewOpen = false;
			goto(`${base}/profiles/${p.id}`);
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			adding = '';
		}
	}

	function openPreview(s) {
		previewOf = s;
		previewOpen = true;
	}

	async function toGallery() {
		await tick();
		gallery?.scrollIntoView({ behavior: 'smooth', block: 'start' });
		gallery?.querySelector('[role="radio"]')?.focus({ preventScroll: true });
	}

	function openNew() {
		newMode = 'choose';
		newOpen = true;
	}
</script>

<div class="page">
	<PageHeader title={t('profiles.title')} subtitle={t('profiles.subtitle')}>
		{#snippet actions()}
			<button type="button" class="btn btn-primary" onclick={openNew} disabled={atLimit} title={atLimit ? t('profiles.err.profile_limit') : undefined} data-testid="new-profile"><Icon name="plus" />{t('profiles.new.title')}</button>
		{/snippet}
	</PageHeader>

	{#if loading}
		<LoadingState />
	{:else if loadError && !profiles.length}
		<div class="card"><ErrorState message={loadError} onretry={load} /></div>
	{:else}
		<div class="pgrid" data-testid="profile-list">
			{#each profiles as p (p.id)}
				<ProfileCard profile={p} href="{base}/profiles/{p.id}" {ctx} />
			{/each}
		</div>

		<div id="default-profiles" class="mt-4">
			<DefaultProfilesCard {profiles} onsaved={load} />
		</div>

		<section class="mt-6" aria-labelledby="gallery-h" bind:this={gallery} data-testid="gallery">
			<h2 id="gallery-h" class="t-section">{t('profiles.gallery.title')}</h2>
			<p class="t-help mb-3">{t('profiles.gallery.hint')}</p>
			<div class="mb-3 filt">
				<RadioSeg size="even full" label={t('profiles.gallery.filter')} value={filter} options={FILTERS.map((f) => ({ value: f, label: t(`profiles.gallery.groups.${f}`) }))} onchange={(v) => (filter = v)} testid="gallery-filter" />
			</div>
			<div class="pgrid">
				{#each shown as s (s.id)}
					{@const badges = badgesOf(s)}
					{@const upd = updateFor(s)}
					<article class="card scard" aria-labelledby="sg-{s.id}" data-testid="suggestion-{s.id}" data-eq="suggestions">
						<div class="sh">
							<ProfileIcon icon={s.icon} color={s.color} />
							<h3 id="sg-{s.id}" class="t-card clamp2">{textOf(s.name)}</h3>
						</div>
						<div class="badges" aria-label={badges.map((b) => b.text).join(', ')}>
							{#each badges as b (b.id)}<span class="badge {b.tone}" data-testid="sbadge-{b.id}"><Icon name={b.icon} /><span class="truncate">{b.text}</span></span>{/each}
						</div>
						<p class="t-help clamp3">{textOf(s.description)}</p>
						<div class="btn2">
							{#if upd}
								<a class="btn btn-sm" href="{base}/profiles/{upd.id}?update=1" data-testid="suggestion-update-{s.id}"><span class="truncate">{t('profiles.gallery.whatsNew')}</span></a>
							{:else}
								<button type="button" class="btn btn-sm" onclick={() => openPreview(s)} aria-label={t('profiles.gallery.previewOf', { name: textOf(s.name) })}><span class="truncate">{t('profiles.gallery.preview')}</span></button>
							{/if}
							<button type="button" class="btn btn-sm btn-primary" onclick={() => add(s)} disabled={!!adding || atLimit} aria-label={t('profiles.gallery.addOf', { name: textOf(s.name) })} data-testid="suggestion-add-{s.id}"><span class="truncate">{t('profiles.gallery.add')}</span></button>
						</div>
					</article>
				{/each}
			</div>
		</section>
	{/if}
</div>

<NewProfileDialog bind:open={newOpen} bind:mode={newMode} {clients} onsuggestion={toGallery} />

<Modal bind:open={previewOpen} title={previewOf ? textOf(previewOf.name) : ''} description={previewOf ? textOf(previewOf.description) : ''} size="md" testid="suggestion-preview">
	{#if previewOf}
		<dl class="dl box">
			<dt>{t('profiles.gallery.for')}</dt>
			<dd>{t(`profiles.access.${previewOf.intended_for}`)}</dd>
			<dt>{t('profiles.childProtection')}</dt>
			<dd>{previewOf.child_protection ? t('profiles.v.on') : t('profiles.v.off')}</dd>
			{#if previewOf.rules}
				{#each Object.entries(previewOf.rules) as [key, value] (key)}
					<dt>{L(ruleOf(key)?.label) || key}</dt>
					<dd>{formatValue(key, value, ctx)}</dd>
				{/each}
			{/if}
		</dl>
		<p class="t-help mt-3">{t('profiles.gallery.copyHint')}</p>
	{/if}
	{#snippet footer({ close })}
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close}>{t('common.close')}</button>
			<button type="button" class="btn btn-primary" onclick={() => add(previewOf)} disabled={!!adding || atLimit} data-testid="suggestion-preview-add"><Icon name="plus" />{t('profiles.gallery.add')}</button>
		</div>
	{/snippet}
</Modal>

<style>
	/* Equal sizes: every card of a grid equally large (rows too). */
	.pgrid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 250px), 1fr));
		grid-auto-rows: 1fr;
		gap: 12px;
	}
	.scard {
		display: grid;
		grid-template-rows: auto auto 1fr auto;
		gap: 10px;
		min-width: 0;
	}
	.sh {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 10px;
		align-items: center;
	}
	.sh h3 {
		margin: 0;
	}
	.clamp2 {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: calc(2 * 1.35em);
		line-height: 1.35;
	}
	.clamp3 {
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: calc(3 * 1.45em);
		line-height: 1.45;
		margin: 0;
	}
	/* Badges: at most two lines, the place is reserved. */
	.badges {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 6px;
		overflow: hidden;
		height: 48px;
		align-content: flex-start;
		min-width: 0;
	}
	.badges .badge {
		min-width: 0;
	}
	.btn2 {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
	}
	.btn2 .btn {
		justify-content: center;
		min-width: 0;
	}
	.filt :global(.seg) {
		max-width: 640px;
		width: 100%;
		grid-auto-flow: row;
		grid-template-columns: repeat(5, minmax(0, 1fr));
	}
	@media (max-width: 560px) {
		.filt :global(.seg) {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}
	}
</style>
