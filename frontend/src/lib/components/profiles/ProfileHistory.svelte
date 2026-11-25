<script>
	/**
	 * History of the profile model (audit with before/after): for one device
	 * or one profile. GET /profile-audit?client_id=|profile_id=;
	 * hidden when the hub does not answer.
	 */
	import { api } from '$lib/api.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { formatDate, formatRelativeDate, formatWeekdayTime } from '$lib/utils.js';
	import Icon from '$lib/ui/Icon.svelte';
	import { L, ruleOf } from '$lib/profiles/catalog.svelte.js';
	import { formatValue } from '$lib/profiles/format.js';

	let { clientId = null, profileId = null, ctx = {}, profiles = [], reload = 0, limit = 20 } = $props();

	let entries = $state(null);

	async function load() {
		try {
			const r = await api.getProfileAudit({ client_id: clientId, profile_id: profileId, limit });
			entries = Array.isArray(r?.entries) ? r.entries : null;
		} catch {
			entries = null;
		}
	}

	$effect(() => {
		void reload;
		load();
	});

	const pname = (id) => profiles.find((p) => p.id === id)?.name || '–';
	const val = (key, v) => (v && typeof v === 'object' && 'value' in v ? formatValue(key, v.value, ctx) : v === null || v === undefined ? '–' : formatValue(key, v, ctx));

	function line(e) {
		const rule = e.rule ? L(ruleOf(e.rule)?.label) || e.rule : '';
		switch (e.action) {
			case 'override_set': {
				const until = e.after?.expires_at ? ` · ${t('profiles.audit.until', { date: formatWeekdayTime(e.after.expires_at) })}` : '';
				return `${rule}: ${val(e.rule, e.before)} → ${val(e.rule, e.after)}${until}`;
			}
			case 'override_removed':
			case 'override_expired':
				return t(`profiles.audit.${e.action}`, { rule, value: val(e.rule, e.before) });
			case 'profile_assigned':
				return t('profiles.audit.profile_assigned', { from: pname(e.before?.profile_id), to: pname(e.after?.profile_id) });
			case 'own_area_set':
				return t('profiles.audit.own_area_set', { area: e.after?.own_area_id ? (ctx.areaName?.(e.after.own_area_id) ?? e.after.own_area_id) : t('profiles.device.noOwnArea') });
			default: {
				const k = `profiles.audit.${e.action}`;
				const s = t(k, { rule });
				return s === k ? e.action : s;
			}
		}
	}
</script>

{#if entries && entries.length}
	<section class="card" aria-labelledby="hist-h-{clientId || profileId}" data-testid="profile-history">
		<div class="card-head"><Icon name="history" /><h2 id="hist-h-{clientId || profileId}">{t('profiles.audit.title')}</h2></div>
		<ol class="feed">
			{#each entries as e (e.id)}
				<li>
					<time class="t-help" datetime={e.ts} title={formatDate(e.ts)}>{formatRelativeDate(e.ts)}</time>
					<span class="min-w-0 flex-1">
						{line(e)}{#if e.note && e.note !== 'removed'}{' '}{t('profiles.audit.note', { note: e.note })}{/if}
						<small class="t-help"> · {e.actor || '–'}</small>
					</span>
				</li>
			{/each}
		</ol>
	</section>
{/if}

<style>
	.feed {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
	}
	.feed li {
		display: grid;
		grid-template-columns: 7.5rem minmax(0, 1fr);
		gap: 10px;
		padding: 7px 0;
		border-bottom: 1px solid var(--line);
		font-size: var(--fs-sm);
	}
	.feed li:last-child {
		border-bottom: 0;
	}
	@media (max-width: 480px) {
		.feed li {
			grid-template-columns: minmax(0, 1fr);
			gap: 2px;
		}
	}
</style>
