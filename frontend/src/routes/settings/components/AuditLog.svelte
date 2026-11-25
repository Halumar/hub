<script>
	/**
	 * Audit view: what admins changed, admin requests and security
	 * events since the hub started, newest first (GET /admin/audit). The
	 * add-on log keeps the full history.
	 */
	import { onMount } from 'svelte';
	import { api } from '$lib/api.js';
	import { formatDate, formatRelativeDate } from '$lib/utils.js';
	import { t, DICTS } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';

	// View -> categories asked from the hub.
	const VIEWS = {
		changes: ['action', 'change'],
		security: ['security'],
		all: [],
	};
	const PREVIEW = 25;

	let view = $state('changes');
	let entries = $state([]);
	let max = $state(500);
	let loading = $state(true);
	let error = $state('');
	let expanded = $state(false);
	let filter = $state('');

	onMount(load);

	async function load() {
		loading = entries.length === 0;
		try {
			const r = await api.getAudit(200, VIEWS[view]);
			entries = r?.entries || [];
			if (Number.isInteger(r?.max_entries)) max = r.max_entries;
			error = '';
		} catch (e) {
			error = t('system.audit.failed', { message: e.message });
		} finally {
			loading = false;
		}
	}

	function setView(v) {
		view = v;
		expanded = false;
		load();
	}

	function icon(e) {
		if (e.category === 'security') return 'warn';
		if (e.category === 'action') return 'checkcircle';
		if (e.category === 'read') return 'eye';
		return 'edit';
	}

	const KNOWN_ACTIONS = new Set(Object.keys(DICTS.en.system.audit.actions));
	const KNOWN_AUTH = new Set(Object.keys(DICTS.en.system.audit.auth));
	const KNOWN_EVENTS = new Set(Object.keys(DICTS.en.system.audit.events));

	/** Readable title: known admin actions get a text, else the raw action. */
	function title(e) {
		if (e.event === 'admin_action') return KNOWN_ACTIONS.has(e.action) ? t(`system.audit.actions.${e.action}`) : e.action;
		if (e.event === 'admin_access') return e.action;
		return KNOWN_EVENTS.has(e.event) ? t(`system.audit.events.${e.event}`) : e.event;
	}

	function who(e) {
		const auth = e.auth && (KNOWN_AUTH.has(e.auth) ? t(`system.audit.auth.${e.auth}`) : e.auth);
		return [e.user, auth, e.ip].filter(Boolean).join(' · ');
	}

	let filtered = $derived.by(() => {
		const q = filter.trim().toLowerCase();
		if (!q) return entries;
		return entries.filter((e) => [title(e), e.details, e.user, e.ip, e.client_id, e.action].some((v) => (v || '').toLowerCase().includes(q)));
	});
	let shown = $derived(expanded ? filtered : filtered.slice(0, PREVIEW));
</script>

<section class="card" aria-labelledby="audit-h" data-testid="audit-card">
	<div class="card-head">
		<Icon name="history" />
		<h2 id="audit-h" class="flex-1">{t('system.audit.title')}</h2>
		<button type="button" class="btn btn-sm" onclick={load} disabled={loading}><Icon name="refresh" />{t('system.refresh')}</button>
	</div>
	<p class="t-help mb-3">{t('system.audit.hint', { count: max })}</p>
	<div class="bar mb-3">
		<Segmented
			size="even"
			label={t('system.audit.view')}
			testid="audit-view"
			options={[
				{ value: 'changes', label: t('system.audit.views.changes') },
				{ value: 'security', label: t('system.audit.views.security') },
				{ value: 'all', label: t('system.audit.views.all') },
			]}
			value={view}
			onchange={setView}
		/>
		<label class="sr-only" for="audit-filter">{t('system.audit.filter')}</label>
		<input id="audit-filter" class="input" type="search" bind:value={filter} placeholder={t('system.audit.filterPlaceholder')} />
	</div>
	{#if loading}
		<LoadingState inline />
	{:else if error}
		<p class="t-help" role="alert">{error}</p>
	{:else if filtered.length === 0}
		<p class="t-help">{t('system.audit.empty')}</p>
	{:else}
		<ol class="feed" data-testid="audit-feed">
			{#each shown as e (e.id)}
				<li class:warn={e.level === 'warning'} data-category={e.category}>
					<Icon name={icon(e)} />
					<span class="min-w-0 flex-1">
						<b class="ttl">{title(e)}</b>
						{#if e.details}<small class="det t-mono">{e.details}</small>{/if}
						{#if who(e)}<small class="t-help block">{who(e)}</small>{/if}
					</span>
					<time class="t-help" datetime={e.ts} title={formatDate(e.ts)}>{formatRelativeDate(e.ts)}</time>
				</li>
			{/each}
		</ol>
		{#if filtered.length > PREVIEW}
			<button type="button" class="btn btn-ghost btn-sm mt-2" onclick={() => (expanded = !expanded)}>
				{expanded ? t('devices.activityLess') : t('devices.activityMore', { count: filtered.length })}
			</button>
		{/if}
	{/if}
</section>

<style>
	.bar {
		display: grid;
		grid-template-columns: minmax(0, 22rem) minmax(0, 1fr);
		gap: 8px;
		align-items: center;
	}
	.bar :global(.seg) {
		width: 100%;
	}
	.feed {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		max-height: 480px;
		overflow: auto;
	}
	.feed li {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 7px 0;
		border-bottom: 1px solid var(--line);
		font-size: var(--fs-sm);
	}
	.feed li:last-child {
		border-bottom: 0;
	}
	.feed li :global(.ic) {
		width: 16px;
		height: 16px;
		margin-top: 2px;
		color: var(--ink-3);
		flex: none;
	}
	.feed li.warn :global(.ic) {
		color: var(--warn-ink);
	}
	.ttl {
		display: block;
		font-weight: 650;
		overflow-wrap: anywhere;
	}
	.det {
		display: block;
		color: var(--ink-2);
		overflow-wrap: anywhere;
	}
	.feed time {
		white-space: nowrap;
	}
	@media (max-width: 639px) {
		.bar {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
