<script>
	/**
	 * Log tab: timeline of activity, changes and alarms grouped by day,
	 * filters (type, period, device, person, profile, area, search), alarm
	 * banner with "mark as read", quick-filter tiles, detail panel. Phones load
	 * the last 7 days first and older entries with "Load more".
	 */
	import { onMount, onDestroy } from 'svelte';
	import { api } from '$lib/api.js';
	import { t, i18n } from '$lib/i18n/index.svelte.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { debounce } from '$lib/utils.js';
	import Icon from '$lib/ui/Icon.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import LogHeader from '$lib/components/log/LogHeader.svelte';
	import EntryPanel from '$lib/components/log/EntryPanel.svelte';
	import {
		alarmType,
		categoryLabel,
		dayKey,
		dayLabel,
		iconOf,
		KIND_CHIP,
		lineParts,
		locale,
		outageMinutes,
		pathLabel,
		reasonText,
		setNamespace,
		timeLabel,
		viaLabel,
		whereKey,
	} from '$lib/log/describe.js';
	import { activeFilterCount, freshFilters, logState, queryOf } from '$lib/log/state.svelte.js';

	const PAGE = 100;

	let summary = $state(null);
	let entries = $state([]);
	let hasMore = $state(false);
	let widened = $state(false);
	let loading = $state(true);
	let loadingMore = $state(false);
	let error = $state('');
	let mobile = $state(false);
	let moreOpen = $state(false);
	let panelOpen = $state(false);
	let panelEntry = $state(null);
	let search = $state(logState.filters.q);

	const f = logState.filters;
	let unsubs = [];

	onMount(() => {
		const mq = window.matchMedia('(max-width: 639px)');
		mobile = mq.matches;
		if (mobile && !logState.mobileDefaultApplied) {
			logState.mobileDefaultApplied = true;
			if (f.range === 'all') f.range = '7d';
		}
		const onMq = (e) => (mobile = e.matches);
		mq.addEventListener('change', onMq);
		loadSummary();
		load();
		unsubs.push(
			() => mq.removeEventListener('change', onMq),
			onBroadcast('log_entry', liveRefresh),
			onBroadcast('log_read', () => {
				loadSummary();
				load();
			}),
			onBroadcast('resync', liveRefresh),
		);
	});
	onDestroy(() => {
		unsubs.forEach((fn) => fn());
		liveRefresh.cancel();
		searchLater.cancel();
	});

	const liveRefresh = debounce(() => {
		loadSummary();
		if (entries.length <= PAGE && !widened) load(false);
	}, 500);

	async function loadSummary() {
		try {
			summary = await api.getLogSummary();
			setNamespace(summary?.namespace);
		} catch {
			/* the header shows nothing */
		}
	}

	async function load(showSpinner = true) {
		if (showSpinner) loading = true;
		widened = false;
		try {
			const r = await api.getLog(queryOf(f, { limit: PAGE }));
			entries = r.entries;
			hasMore = r.has_more;
			error = '';
		} catch (e) {
			error = t('log.loadFailed', { message: e.message });
		} finally {
			loading = false;
		}
	}

	async function loadMore() {
		if (!entries.length) return;
		loadingMore = true;
		try {
			const extra = { limit: PAGE, before_id: entries.at(-1).id };
			let params = queryOf(f, extra);
			if (!hasMore && f.range !== 'all') {
				widened = true;
				params = queryOf({ ...f, range: 'all' }, extra);
			}
			const r = await api.getLog(params);
			entries = [...entries, ...r.entries];
			hasMore = r.has_more;
			if (!r.entries.length) widened = true;
		} catch (e) {
			addToast(t('log.loadFailed', { message: e.message }), 'error');
		} finally {
			loadingMore = false;
		}
	}

	function setFilter(key, value) {
		f[key] = value;
		load();
	}
	const searchLater = debounce(() => setFilter('q', search.trim()), 300);

	function resetFilters() {
		Object.assign(f, freshFilters(mobile));
		search = '';
		load();
	}

	function statFilter(cat) {
		if (f.category === cat) {
			f.category = '';
			f.range = freshFilters(mobile).range;
		} else {
			f.category = cat;
			f.range = cat === 'activity' ? 'today' : '7d';
		}
		load();
	}

	function showAlarms() {
		Object.assign(f, freshFilters(mobile), { category: 'alarm', range: 'all' });
		search = '';
		load();
	}

	async function markAll() {
		try {
			const r = await api.markLogRead(null);
			addToast(t('log.markedRead', { count: r.marked }), 'success');
			await loadSummary();
			entries = entries.map((e) => ({ ...e, unread: false }));
		} catch (e) {
			addToast(t('log.saveFailed', { message: e.message }), 'error');
		}
	}

	async function markOne(entry) {
		try {
			await api.markLogRead([entry.id]);
			addToast(t('log.readOne'), 'success');
			entries = entries.map((e) => (e.id === entry.id ? { ...e, unread: false } : e));
			panelEntry = { ...entry, unread: false };
			loadSummary();
		} catch (e) {
			addToast(t('log.saveFailed', { message: e.message }), 'error');
		}
	}

	function openEntry(entry) {
		panelEntry = entry;
		panelOpen = true;
	}

	function openUnread(u) {
		const e = entries.find((x) => x.id === u.id);
		if (e) openEntry(e);
		else showAlarms();
	}

	let nFilters = $derived(activeFilterCount(f, mobile));
	let days = $derived.by(() => {
		const out = [];
		for (const e of entries) {
			const k = dayKey(e.ts);
			if (!out.length || out.at(-1).key !== k) out.push({ key: k, items: [] });
			out.at(-1).items.push(e);
		}
		return out;
	});
	let facets = $derived(summary?.facets || { devices: [], owners: [], profiles: [], areas: [] });
	let retention = $derived(summary?.retention_days || 30);
	let hidden = $derived(summary?.hidden_owners || []);
	let filterSelectsOn = $derived(['client_id', 'owner', 'profile_id', 'area_id'].some((k) => f[k]));

	function ariaOf(e) {
		const kind = categoryLabel(e.category) + (e.category === 'alarm' && alarmType(e) ? ` ${alarmType(e)}` : '');
		return `${timeLabel(e.ts)}, ${kind}${e.unread ? `, ${t('log.new').toLowerCase()}` : ''}: ${lineParts(e).join(' · ')}`;
	}
	function oldestDate() {
		return summary?.oldest_ts ? new Date(summary.oldest_ts).toLocaleDateString(locale()) : '–';
	}
	const SELECTS = [
		['client_id', 'log.device', () => facets.devices.map((d) => [d.id, d.name])],
		['owner', 'log.person', () => facets.owners.map((o) => [o, o])],
		['profile_id', 'log.profile', () => facets.profiles.map((p) => [p.id, p.name])],
		['area_id', 'log.area', () => facets.areas.map((a) => [a.id, a.name])],
	];
</script>

<svelte:head><title>{t('log.title')}</title></svelte:head>

<div class="page logpage" data-testid="log-page" lang={i18n.locale}>
	<LogHeader active="list" {summary} />

	{#if summary?.unread_alarms}
		<div class="banner banner-warn newalarms" role="region" aria-label={t('log.newAlarms', { count: summary.unread_alarms })} data-testid="log-alarm-banner">
			<Icon name="warn" />
			<div class="banner-body">
				<b>{t('log.newAlarms', { count: summary.unread_alarms })}</b>
				<ul class="newlist">
					{#each summary.unread.slice(0, 5) as u (u.id)}
						<li>
							<button type="button" class="link" onclick={() => openUnread(u)}>
								{timeLabel(u.ts)} · {t(`log.alarmTypes.${u.kind}`)}{u.device_name ? ` · ${u.device_name}` : ''}
							</button>
						</li>
					{/each}
				</ul>
			</div>
			<div class="banner-actions">
				<div class="btnrow" data-eq="bannerbtns">
					<button type="button" class="btn btn-sm" onclick={showAlarms} data-testid="log-show-alarms">{t('log.showAlarms')}</button>
					<button type="button" class="btn btn-sm" onclick={markAll} data-testid="log-mark-read"><Icon name="check" />{t('log.markRead')}</button>
				</div>
			</div>
		</div>
	{/if}

	{#if summary && summary.total > 0}
		<div class="tiles lstats" data-eq="stats">
			{#each [['activity', summary.counts.activity_today, 'log.statActivity'], ['change', summary.counts.changes_7d, 'log.statChanges'], ['alarm', summary.counts.alarms_7d, 'log.statAlarms']] as [cat, n, label] (cat)}
				<button type="button" class="tile lstat" aria-pressed={f.category === cat} onclick={() => statFilter(cat)} data-testid="log-stat-{cat}">
					<span class="tile-row"><span class="chipic sm {KIND_CHIP[cat]}"><Icon name={cat === 'alarm' ? 'warn' : cat === 'change' ? 'edit' : 'power'} /></span><b class="num">{n}</b></span>
					<small>{t(label)}</small>
				</button>
			{/each}
		</div>

		<section class="card lfilter" aria-label={t('log.filter')}>
			<div class="lfrow">
				<div class="seg even" role="group" aria-label={t('log.kindLabel')} data-eq="kind" data-testid="log-kind">
					{#each [['', 'log.all'], ['activity', 'log.activity'], ['change', 'log.changes'], ['alarm', 'log.alarms']] as [v, k] (v)}
						<button type="button" aria-pressed={f.category === v} onclick={() => setFilter('category', v)}>{t(k)}</button>
					{/each}
				</div>
				<div class="seg even" role="group" aria-label={t('log.periodLabel')} data-eq="range" data-testid="log-range">
					{#each [['today', t('log.today')], ['7d', t('log.days7')], ['all', t('log.allDays', { days: retention })]] as [v, label] (v)}
						<button type="button" aria-pressed={f.range === v} aria-label={label} onclick={() => setFilter('range', v)}>{v === 'all' && mobile ? t('log.allShort') : label}</button>
					{/each}
				</div>
				<div class="lsearch">
					<label class="sr-only" for="log-q">{t('log.searchLabel')}</label>
					<Icon name="search" />
					<input class="input" id="log-q" type="search" bind:value={search} oninput={searchLater} placeholder={t('log.searchPlaceholder')} data-testid="log-search" />
				</div>
			</div>
			{#if mobile}
				<button type="button" class="btn btn-sm morebtn" aria-expanded={moreOpen} onclick={() => (moreOpen = !moreOpen)}>
					<Icon name="sliders" />{t('log.moreFilters')}{filterSelectsOn ? ' •' : ''}
				</button>
			{/if}
			{#if !mobile || moreOpen}
				<div class="lsel" data-eq="selects">
					{#each SELECTS as [key, label, opts] (key)}
						<label class="field">
							<span class="field-label">{t(label)}</span>
							<select class="select" value={f[key]} onchange={(e) => setFilter(key, e.currentTarget.value)} data-testid="log-f-{key}">
								<option value="">{t('log.all')}</option>
								{#each opts() as [v, text] (v)}<option value={v}>{text}</option>{/each}
							</select>
						</label>
					{/each}
				</div>
			{/if}
			{#if nFilters}
				<div class="row">
					<span class="t-help">{t('log.activeFilters', { count: nFilters })}</span>
					<button type="button" class="btn btn-ghost btn-sm" onclick={resetFilters} data-testid="log-reset"><Icon name="x" />{t('log.resetFilters')}</button>
				</div>
			{/if}
		</section>
	{/if}

	{#if loading && !entries.length}
		<LoadingState />
	{:else if error}
		<ErrorState message={error} onretry={() => load()} />
	{:else if summary && summary.total === 0}
		<div class="card empty" data-testid="log-empty">
			<Icon name="history" />
			<h2 class="t-card">{t('log.emptyTitle')}</h2>
			<p class="t-sub">{t('log.emptyText', { days: retention })}</p>
		</div>
	{:else if !entries.length}
		{@const alarmsOnly = f.category === 'alarm' && nFilters <= 2}
		<div class="card empty" data-testid="log-nomatch">
			<Icon name={alarmsOnly ? 'checkcircle' : 'search'} />
			<h2 class="t-card">{alarmsOnly ? t('log.noAlarmsTitle') : t('log.noMatchTitle')}</h2>
			<p class="t-sub">{alarmsOnly ? t('log.noAlarmsText') : t('log.noMatchText')}</p>
			<div class="btnrow" style="width:min(100%,240px)"><button type="button" class="btn" onclick={resetFilters}>{t('log.resetFilters')}</button></div>
		</div>
	{:else}
		<p class="t-help" aria-live="polite" data-testid="log-count">
			{t('log.entries', { count: entries.length })}{hasMore ? '+' : ''}
			{#if hidden.length} · {t('log.hiddenNote', { names: hidden.join(', ') })}{/if}
		</p>
		{#each days as day (day.key)}
			<section class="tl-day" aria-labelledby="day-{day.key}">
				<h2 class="t-overline" id="day-{day.key}">{dayLabel(day.key)}</h2>
				<ol class="tl">
					{#each day.items as e (e.id)}
						{@const where = whereKey(e)}
						<li>
							<button type="button" class="tl-item k-{e.category}" class:new={e.unread} aria-label={ariaOf(e)} onclick={() => openEntry(e)} data-testid="log-entry">
								<span class="tl-time">
									<time datetime={e.ts}>{timeLabel(e.ts)}</time>
									{#if e.unread}<span class="tl-new" title={t('log.new')}></span>{/if}
								</span>
								<span class="chipic sm {KIND_CHIP[e.category]}"><Icon name={iconOf(e)} /></span>
								<span class="tl-main">
									<span class="tl-line"><b>{lineParts(e)[0]}</b> · {lineParts(e)[1]}</span>
									{#if e.result === 'denied'}
										<span class="tl-reason"><Icon name="lock" />{t('log.notDone')}{#if e.reason}{` · ${t('log.reasonPrefix')}: ${reasonText(e.reason)}`}{/if}</span>
									{/if}
									<span class="tl-sub">
										<span class="tl-kind">{e.category === 'alarm' ? alarmType(e) || categoryLabel(e.category) : categoryLabel(e.category)}</span>
										{#if where}<span class:tl-away={where === 'away'}>{where === 'away' ? t('log.away') : t('log.atHome')}</span>{/if}
										{#if e.kind === 'override_set' && e.detail?.after?.expires_at}<span>{t('log.temporary')}</span>{/if}
										{#if e.kind === 'integration_outage' && outageMinutes(e)}<span>{t('log.lasted', { minutes: outageMinutes(e) })}</span>{/if}
										{#if e.profile_name}<span>{t('log.profile')}: {e.profile_name}</span>{/if}
										{#if e.category === 'activity' && (viaLabel(e) || pathLabel(e))}<span class="tl-via">{[viaLabel(e), pathLabel(e)].filter(Boolean).join(' · ')}</span>{/if}
									</span>
								</span>
								<Icon name="chev" class="tl-chev" />
							</button>
						</li>
					{/each}
				</ol>
			</section>
		{/each}
		{#if hasMore || (!widened && f.range !== 'all')}
			<div class="btnrow more" style="width:min(100%,260px)">
				<button type="button" class="btn" onclick={loadMore} disabled={loadingMore} data-testid="log-more">{hasMore ? t('log.loadMore') : t('log.loadOlder')}</button>
			</div>
		{/if}
		<p class="t-help lfoot"><Icon name="lock" /> {t('log.footer', { date: oldestDate(), days: retention })}</p>
	{/if}

	<EntryPanel bind:open={panelOpen} entry={panelEntry} onread={markOne} />
</div>

<style>
	.newalarms {
		margin-bottom: var(--s4);
	}
	.newlist {
		margin: 4px 0 0;
		padding-left: 18px;
		font-size: var(--fs-sm);
	}
	.lstats {
		margin-bottom: var(--s3);
	}
	.lstat {
		min-height: 76px !important;
	}
	.lfilter {
		display: grid;
		gap: var(--s3);
		margin-bottom: var(--s4);
	}
	.lfrow {
		display: flex;
		flex-wrap: wrap;
		gap: var(--s2);
		align-items: center;
	}
	.lsearch {
		position: relative;
		flex: 1 1 220px;
		min-width: 0;
	}
	.lsearch :global(.ic) {
		position: absolute;
		left: 10px;
		top: 50%;
		transform: translateY(-50%);
		width: 16px;
		height: 16px;
		color: var(--ink-3);
		pointer-events: none;
	}
	.lsearch .input {
		width: 100%;
		padding-left: 32px;
	}
	.lsel {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: var(--s2);
	}
	.lsel .select {
		width: 100%;
		min-width: 0;
	}
	.morebtn {
		justify-self: start;
	}
	.empty {
		display: grid;
		justify-items: center;
		text-align: center;
		gap: var(--s2);
		padding: var(--s6, 32px) var(--s4);
	}
	.empty > :global(.ic) {
		width: 32px;
		height: 32px;
		color: var(--ink-3);
	}
	.tl-day {
		margin: var(--s4) 0 var(--s2);
	}
	.tl-day h2 {
		margin: 0 0 var(--s2);
	}
	.tl {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 6px;
	}
	.tl-item {
		display: grid;
		grid-template-columns: 56px 28px minmax(0, 1fr) 16px;
		align-items: start;
		gap: 12px;
		width: 100%;
		padding: 12px 14px;
		border-radius: var(--r-box);
		border: 1px solid var(--line);
		background: var(--surface);
		color: var(--ink);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.tl-item:hover {
		border-color: var(--line-2);
	}
	.tl-item:focus-visible {
		outline: 2px solid var(--focus);
		outline-offset: 2px;
	}
	.tl-item.new {
		background: color-mix(in srgb, var(--warn-soft) 55%, var(--surface));
		border-color: color-mix(in srgb, var(--warn) 22%, var(--line));
	}
	.tl-time {
		display: flex;
		align-items: center;
		gap: 6px;
		padding-top: 4px;
	}
	.tl-time time {
		font-variant-numeric: tabular-nums;
		font-size: var(--fs-sm);
		font-weight: 650;
		color: var(--ink-2);
	}
	.tl-new {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--warn);
		flex: none;
	}
	.tl-main {
		display: grid;
		gap: 3px;
		min-width: 0;
		padding-top: 3px;
	}
	.tl-line {
		font-size: var(--fs-body);
		line-height: 1.4;
		overflow-wrap: anywhere;
	}
	.tl-reason {
		display: flex;
		align-items: flex-start;
		gap: 5px;
		font-size: var(--fs-sm);
		color: var(--warn-ink);
		overflow-wrap: anywhere;
	}
	.tl-reason :global(.ic) {
		width: 12px;
		height: 12px;
		flex: none;
		margin-top: 0.2em;
	}
	.tl-sub {
		display: flex;
		flex-wrap: wrap;
		gap: 2px 0;
		font-size: var(--fs-cap);
		line-height: 1.45;
		color: var(--ink-3);
		overflow-wrap: anywhere;
	}
	.tl-sub > span:not(:last-child)::after {
		content: '·';
		margin: 0 6px;
		color: var(--ink-3);
		font-weight: 400;
	}
	.tl-kind {
		font-weight: 650;
	}
	.k-activity .tl-kind {
		color: var(--info-ink);
	}
	.k-change .tl-kind {
		color: color-mix(in srgb, #5b2fd6 85%, var(--ink));
	}
	:global(:root[data-theme='dark']) .k-change .tl-kind {
		color: #c9b6ff;
	}
	.k-alarm .tl-kind {
		color: var(--warn-ink);
	}
	.tl-away {
		color: var(--info-ink);
		font-weight: 600;
	}
	.tl-item :global(.tl-chev) {
		width: 16px;
		height: 16px;
		color: var(--ink-3);
		margin-top: 6px;
	}
	.more {
		margin: var(--s4) auto 0;
	}
	.lfoot {
		margin-top: var(--s4);
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.lfoot :global(.ic) {
		width: 14px;
		height: 14px;
		flex: none;
	}
	@media (max-width: 639px) {
		.lfrow .seg {
			width: 100%;
			display: grid;
		}
		.lfrow .seg button {
			min-height: 40px;
		}
		.lfrow .seg[data-eq='kind'] {
			grid-template-columns: repeat(2, minmax(0, 1fr));
			grid-auto-flow: row;
		}
		.tiles.lstats {
			--cols: 3;
		}
		.tiles.lstats :global(.tile) {
			padding: 10px;
		}
		.newalarms .btnrow {
			grid-auto-flow: row;
		}
		.lsel {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.tl-item {
			grid-template-columns: 52px minmax(0, 1fr) 14px;
			gap: 8px;
			padding: 12px;
		}
		.tl-item .chipic {
			display: none;
		}
		.tl-sub > span:nth-last-child(2):has(+ .tl-via)::after,
		.tl-via {
			display: none;
		}
	}
</style>
