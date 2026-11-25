<script>
	/**
	 * Detail of a log entry: side panel on wide screens, bottom sheet
	 * on phones (Modal). Who, device, profile, where, area, via, control path,
	 * result, reason, before → after, notified targets, honest notes.
	 */
	import { base } from '$app/paths';
	import { t } from '$lib/i18n/index.svelte.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import {
		alarmType,
		categoryLabel,
		dateTimeLabel,
		lineParts,
		outageMinutes,
		pathLabel,
		reasonText,
		relLabel,
		viaLabel,
		whereKey,
		whoLabel,
	} from '$lib/log/describe.js';

	let { open = $bindable(false), entry = null, onread = null } = $props();

	let title = $derived(entry ? categoryLabel(entry.category) + (entry.category === 'alarm' && alarmType(entry) ? ` · ${alarmType(entry)}` : '') : '');

	function fmt(v) {
		if (v === null || v === undefined) return '–';
		if (typeof v === 'object') {
			if (!Array.isArray(v) && 'value' in v) return fmt(v.value) + (v.expires_at ? ` (${t('log.until')} ${dateTimeLabel(v.expires_at)})` : '');
			const s = JSON.stringify(v);
			return s.length > 80 ? `${s.slice(0, 77)}…` : s;
		}
		return String(v);
	}

	function isPlain(v) {
		return v && typeof v === 'object' && !Array.isArray(v) && !('value' in v);
	}

	/** Changed leaves (two levels, e.g. rules.kids_quiet_hours), at most 12. */
	function changed(b, a, prefix = '', depth = 0, out = []) {
		const keys = [...new Set([...Object.keys(b || {}), ...Object.keys(a || {})])].sort();
		for (const k of keys) {
			const x = b?.[k],
				y = a?.[k];
			if (JSON.stringify(x) === JSON.stringify(y)) continue;
			if (depth < 1 && isPlain(x) && isPlain(y)) changed(x, y, `${prefix}${k}.`, depth + 1, out);
			else out.push([`${prefix}${k}`, fmt(x), fmt(y)]);
		}
		return out;
	}

	let diff = $derived.by(() => {
		const d = entry?.detail;
		if (!d || (d.before == null && d.after == null)) return [];
		const b = d.before,
			a = d.after;
		if (isPlain(b) && isPlain(a)) return changed(b, a).slice(0, 12);
		// created or deleted object: the name is in the line, the body is not shown
		if (isPlain(b) || isPlain(a)) return [];
		return [[d.rule || '', fmt(b), fmt(a)]];
	});

	let rows = $derived.by(() => {
		const e = entry;
		if (!e) return [];
		const w = whereKey(e);
		const res = e.result === 'denied' ? t('log.notDone') : e.result === 'ok' ? t('log.done') : '';
		return [
			[t('log.when'), `${dateTimeLabel(e.ts)} (${relLabel(e.ts)})`],
			[e.category === 'change' ? t('log.who') : t('log.person'), e.category === 'change' ? whoLabel(e) : e.owner_label || (e.client_id ? t('log.sharedDevice') : '')],
			[t('log.profile'), e.profile_name ? e.profile_name + (e.profile_exists ? '' : ` ${t('log.profileGone')}`) : ''],
			[t('log.where'), w ? (w === 'away' ? t('log.away') : t('log.atHome')) : ''],
			[t('log.area'), e.area_name || ''],
			[t('log.entity'), e.entity_id ? (e.entity_name ? `${e.entity_name} · ${e.entity_id}` : e.entity_id) : ''],
			[t('log.usedVia'), e.category === 'activity' ? viaLabel(e) : ''],
			[t('log.controlPath'), e.category === 'activity' ? pathLabel(e) : ''],
			[t('log.result'), res],
			[e.category === 'alarm' ? t('log.reason') : t('log.ruleReason'), reasonText(e.reason)],
			[t('log.count'), e.count && e.count > 1 ? String(e.count) : ''],
			[t('log.since'), e.since ? dateTimeLabel(e.since) : ''],
			[t('log.until'), e.until ? dateTimeLabel(e.until) : e.kind === 'integration_outage' && e.source === 'hub' ? t('log.running') : ''],
		].filter((r) => r[1]);
	});

	let note = $derived.by(() => {
		const e = entry;
		if (!e) return '';
		if (e.control_path === 'direct') return t('log.directNote');
		if (e.kind === 'access_group_restored') return t('log.groupRestoredNote');
		if (e.kind === 'integration_outage') return t('log.outageNote');
		if (e.detail?.reliability === 'best_effort') return t('log.reliability');
		return '';
	});
</script>

<Modal bind:open {title} description={entry ? dateTimeLabel(entry.ts) : ''} size="md" panelClass="log-panel" testid="log-panel">
	{#if entry}
		<div class="stack">
			<div class="box">
				<p class="line" data-testid="log-panel-line"><b>{lineParts(entry)[0]}</b> · {lineParts(entry).slice(1).join(' · ')}</p>
				{#if entry.unread}<span class="badge badge-accent"><Icon name="sparkle" />{t('log.new')}</span>{/if}
				{#if outageMinutes(entry) && entry.kind === 'integration_outage'}<span class="badge"><Icon name="clock" />{t('log.lasted', { minutes: outageMinutes(entry) })}</span>{/if}
			</div>
			<dl class="dl">
				{#if entry.client_id || entry.device_name}
					<dt>{t('log.device')}</dt>
					<dd>
						{#if entry.client_id && entry.device_exists}
							<a class="link" href="{base}/clients/{entry.client_id}">{entry.device_name}</a>
						{:else}
							{entry.device_name || '–'} <span class="t-muted">({t('log.deviceGone')})</span>
						{/if}
					</dd>
				{/if}
				{#each rows as [k, v] (k)}
					<dt>{k}</dt>
					<dd>{v}</dd>
				{/each}
			</dl>
			{#if diff.length}
				<div class="stack-sm">
					<h3 class="t-label">{t('log.beforeAfter')}</h3>
					<ul class="diff">
						{#each diff as [k, b, a], i (i)}
							<li><Icon name="sliders" /><span>{#if k}<b>{k}</b>{': '}{/if}<del>{b}</del> → <ins>{a}</ins></span></li>
						{/each}
					</ul>
				</div>
			{/if}
			{#if entry.detail?.entities?.length}
				<div class="stack-sm">
					<h3 class="t-label">{t('log.details')}</h3>
					<ul class="diff">
						{#each entry.detail.entities as ent (ent)}<li><Icon name="chev" /><span class="t-mono">{ent}</span></li>{/each}
					</ul>
				</div>
			{/if}
			{#if entry.detail?.sent?.length}
				<div class="stack-sm">
					<h3 class="t-label">{t('log.notified')}</h3>
					<ul class="diff" data-testid="log-panel-sent">
						{#each entry.detail.sent as s, i (i)}
							<li>
								<Icon name={s.target.startsWith('notify.') ? 'bell' : 'iphone'} />
								<span><b class:t-mono={s.target.startsWith('notify.')}>{s.name || s.target}</b>: {t(`log.sentStatus.${s.status}`)}</span>
							</li>
						{/each}
					</ul>
				</div>
			{/if}
			{#if note}<p class="t-help"><Icon name="info" /> {note}</p>{/if}
		</div>
	{/if}
	{#snippet footer()}
		<div class="foot-eq panel-foot" data-eq="panelfoot">
			{#if entry?.client_id && entry.device_exists}
				<a class="btn" href="{base}/clients/{entry.client_id}"><Icon name="iphone" />{t('log.openDevice')}</a>
			{:else}
				<button type="button" class="btn" disabled>{entry?.client_id || entry?.device_name ? t('log.deviceGone') : t('log.noDevice')}</button>
			{/if}
			{#if entry?.profile_id && entry.profile_exists}
				<a class="btn" href="{base}/profiles/{entry.profile_id}"><Icon name="template" />{t('log.openProfile')}</a>
			{:else}
				<button type="button" class="btn" disabled>{t('log.noProfile')}</button>
			{/if}
			{#if entry?.unread}
				<button type="button" class="btn btn-primary" onclick={() => onread?.(entry)} data-testid="log-panel-read"><Icon name="check" />{t('log.read')}</button>
			{/if}
		</div>
	{/snippet}
</Modal>

<style>
	.line {
		margin: 0 0 6px;
	}
	.box :global(.badge) {
		margin-right: 6px;
	}
	.diff {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 6px;
		font-size: var(--fs-sm);
	}
	.diff li {
		display: flex;
		gap: 8px;
		align-items: flex-start;
		overflow-wrap: anywhere;
	}
	.diff :global(.ic) {
		width: 14px;
		height: 14px;
		flex: none;
		margin-top: 3px;
		color: var(--ink-3);
	}
	del {
		color: var(--err-ink);
	}
	ins {
		color: var(--ok-ink);
		text-decoration: none;
	}
	.panel-foot {
		display: grid;
		grid-auto-flow: column;
		grid-auto-columns: 1fr;
		gap: var(--s2);
		width: 100%;
	}
	.panel-foot :global(.btn) {
		min-width: 0;
		justify-content: center;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	@media (max-width: 639px) {
		.panel-foot {
			grid-auto-flow: row;
		}
	}
</style>
