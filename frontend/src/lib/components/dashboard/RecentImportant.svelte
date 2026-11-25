<script>
	/**
	 * Overview card "Recent important": the last important log
	 * entries of 7 days (alarms, kids' requests, guests, new devices, profile
	 * changes). Unread alarms carry a dot (read marks of the log); a row opens
	 * the log with the matching filter. Live through the admin broadcasts
	 * log_entry / log_read.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { debounce } from '$lib/utils.js';
	import { logState } from '$lib/log/state.svelte.js';
	import { alarmType, dayKey, locale, timeLabel, whatLabel, whoLabel } from '$lib/log/describe.js';
	import { groupOf, isImportant, loadImportant, logFiltersFor } from '$lib/log/important.js';
	import Icon from '$lib/ui/Icon.svelte';

	let { class: cls = '' } = $props();

	let entries = $state([]);
	let loading = $state(true);
	let failed = $state(false);
	let unsubs = [];

	const ICON = { alarm: 'warn', request: 'child', guest: 'users', device: 'iphone', profile: 'edit' };
	const ALARM_ICON = { denied: 'lock', access_group_restored: 'shield', direct_access_attempt: 'shield', integration_outage: 'wifioff' };

	onMount(() => {
		load();
		unsubs.push(
			onBroadcast('log_entry', (e) => {
				if (isImportant(e)) refresh();
			}),
			onBroadcast('log_read', (d) => {
				if (d?.all) entries = entries.map((e) => ({ ...e, unread: false }));
				else if (d?.ids) entries = entries.map((e) => (d.ids.includes(e.id) ? { ...e, unread: false } : e));
			}),
			onBroadcast('resync', () => refresh()),
		);
	});
	onDestroy(() => {
		refresh.cancel();
		unsubs.forEach((fn) => fn());
	});

	async function load() {
		try {
			entries = await loadImportant(api);
			failed = false;
		} catch {
			failed = true;
		} finally {
			loading = false;
		}
	}
	const refresh = debounce(load, 400);

	function title(e) {
		if (e.category === 'alarm' && e.kind !== 'denied') return alarmType(e) || whatLabel(e);
		return whatLabel(e);
	}

	function when(iso) {
		if (dayKey(iso) === dayKey(new Date().toISOString())) return timeLabel(iso);
		const d = new Date(iso);
		return `${d.toLocaleDateString(locale(), { weekday: 'short' })} ${timeLabel(iso)}`;
	}

	function open(e) {
		const mobile = window.matchMedia('(max-width: 639px)').matches;
		Object.assign(logState.filters, logFiltersFor(e, mobile));
		logState.mobileDefaultApplied = true;
	}
</script>

<section class="card recent {cls}" aria-labelledby="dash-recent" data-testid="dash-recent">
	<div class="card-head">
		<Icon name="history" />
		<h2 id="dash-recent" class="flex-1">{t('dashboard.recent.title')}</h2>
		<a class="btn btn-ghost btn-sm" href="{base}/log" data-testid="dash-recent-all">{t('dashboard.recent.all')}<Icon name="chev" /></a>
	</div>
	{#if loading}
		<p class="quiet" aria-busy="true">{t('dashboard.recent.loading')}</p>
	{:else if failed}
		<div class="quiet" data-testid="dash-recent-error">
			<span>{t('dashboard.recent.error')}</span>
			<button type="button" class="btn btn-sm" onclick={load}><Icon name="refresh" />{t('dashboard.recent.retry')}</button>
		</div>
	{:else if !entries.length}
		<div class="quiet empty" data-testid="dash-recent-empty">
			<Icon name="checkcircle" />
			<span>{t('dashboard.recent.empty')}</span>
		</div>
	{:else}
		<ul class="list">
			{#each entries as e (e.id)}
				{@const g = groupOf(e)}
				<li>
					<a
						class="item"
						href="{base}/log"
						onclick={() => open(e)}
						data-testid="dash-recent-item"
						data-unread={e.unread ? 'true' : undefined}
						title={`${title(e)} · ${whoLabel(e)}`}
					>
						<span class="ricon g-{g}"><Icon name={g === 'alarm' ? ALARM_ICON[e.kind] || 'warn' : ICON[g]} /></span>
						<span class="txt">
							<b>{title(e)}</b>
							<small><time datetime={e.ts}>{when(e.ts)}</time> · {whoLabel(e)}</small>
						</span>
						{#if e.unread}
							<span class="dot" data-testid="dash-recent-dot"></span>
							<span class="sr-only">{t('dashboard.recent.unread')}</span>
						{/if}
					</a>
				</li>
			{/each}
		</ul>
	{/if}
</section>

<style>
	.recent {
		display: flex;
		flex-direction: column;
	}
	.list > li {
		padding: 0;
	}
	.item {
		display: flex;
		align-items: center;
		gap: var(--s3);
		width: 100%;
		min-width: 0;
		padding: 8px 6px;
		margin: 0 -6px;
		box-sizing: content-box;
		border-radius: var(--r-box);
		color: inherit;
		text-decoration: none;
	}
	.item:hover {
		background: var(--fill);
	}
	.item:hover b {
		text-decoration: underline;
	}
	.item:focus-visible {
		outline: 2px solid var(--focus);
		outline-offset: 0;
	}
	.ricon {
		width: 28px;
		height: 28px;
		border-radius: 8px;
		display: grid;
		place-items: center;
		flex: none;
		background: var(--fill-2);
		color: var(--ink-2);
	}
	.ricon :global(.ic) {
		width: 16px;
		height: 16px;
	}
	.g-alarm {
		background: var(--warn-soft);
		color: var(--warn-ink);
	}
	.g-request {
		background: var(--light-soft);
		color: var(--light-ink);
	}
	.g-guest,
	.g-device {
		background: var(--info-soft);
		color: var(--info-ink);
	}
	.txt {
		flex: 1;
		min-width: 0;
	}
	.txt b,
	.txt small {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.txt b {
		font-weight: 650;
	}
	.txt small {
		color: var(--ink-3);
		font-size: var(--fs-cap);
	}
	.txt time {
		font-variant-numeric: tabular-nums;
	}
	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--accent);
		flex: none;
	}
	.quiet {
		display: flex;
		align-items: center;
		gap: var(--s2);
		flex-wrap: wrap;
		margin: 0;
		color: var(--ink-3);
		font-size: var(--fs-sm);
	}
	.empty {
		flex: 1;
		flex-direction: column;
		justify-content: center;
		text-align: center;
		padding: var(--s4) 0;
	}
	.empty :global(.ic) {
		width: 24px;
		height: 24px;
		color: var(--ok-ink);
	}
</style>
