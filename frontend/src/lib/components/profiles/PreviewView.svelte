<script>
	/**
	 * Preview of a profile write (?dry_run=true, profile-preview.response):
	 * per affected device the changed effective values (before → after),
	 * adjustments that would be deleted, HA key and remote access.
	 */
	import Icon from '$lib/ui/Icon.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { L, ruleOf } from '$lib/profiles/catalog.svelte.js';
	import { formatValue, untilText } from '$lib/profiles/format.js';
	import RemoteKeyWarning from './RemoteKeyWarning.svelte';
	import { api } from '$lib/api.js';
	import { hubRemoteOf } from '$lib/profiles/preview.js';

	let { preview, ctx = {}, testid = 'preview' } = $props();

	// Only for a hub that does not send hub_remote_now / hub_remote_at: the devices' current values.
	let clients = $state(null);
	$effect(() => {
		if (clients !== null || !all.some((d) => !Object.prototype.hasOwnProperty.call(d, 'hub_remote_now'))) return;
		api.getPairedClients()
			.then((list) => (clients = Array.isArray(list) ? list : []))
			.catch(() => (clients = []));
	});
	let byId = $derived(Object.fromEntries((clients || []).map((c) => [c.id, c])));

	// The hub lists the devices the change touches first (affected_count counts them), then devices it
	// leaves as they are that are or become "Hub only" with remote access: shown apart, with their warning.
	let all = $derived(preview?.devices || []);
	let devices = $derived(all.slice(0, preview?.affected_count ?? all.length));
	// The hub lists an unchanged device when its pair changes, also to (false, null): only those that warn are shown.
	let listed = $derived(all.slice(preview?.affected_count ?? all.length).filter((d) => d.hub_remote_now || d.hub_remote_at));

	function label(key) {
		return L(ruleOf(key)?.label) || key;
	}

	/** Text of a watch removal reason; an unknown one still says that the watch goes. */
	function watchReason(reason) {
		const key = `profiles.preview.watchReason.${reason}`;
		const s = t(key);
		return s === key ? t('profiles.preview.watchReasonOther') : s;
	}

	function val(key, e) {
		if (!e || e.source === 'not_applicable') return t('profiles.preview.na');
		return formatValue(key, e.value, ctx);
	}
</script>

<div class="pv" data-testid={testid}>
	<p class="lead" data-testid="{testid}-count">
		<Icon name="iphone" />
		{preview?.affected_count ? t('profiles.preview.affects', { count: preview.affected_count }) : t('profiles.preview.none')}
	</p>
	{#each preview?.watches_removed || [] as w (w.client_id)}
		<p class="cons warn" data-testid="{testid}-watch-removed"><Icon name="trash" size="14" />{t('profiles.preview.watchRemoved', { name: w.name, reason: watchReason(w.reason) })}</p>
	{/each}
	{#if devices.length}
		<ul class="devs">
			{#each devices as d (d.client_id)}
				<li class="box dev" data-testid="{testid}-device">
					<b class="block">{d.name}</b>
					{#if d.ha_key === 'removed'}<p class="cons"><Icon name="key" size="14" />{t('profiles.preview.haKeyRemoved')}</p>{/if}
					{#if d.ha_key === 'restored'}<p class="cons"><Icon name="key" size="14" />{t('profiles.preview.haKeyRestored')}</p>{/if}
					{#if hubRemoteOf(d, byId[d.client_id]).now}<RemoteKeyWarning testid="{testid}-remote-key-warning" />{:else if hubRemoteOf(d, byId[d.client_id]).at}<RemoteKeyWarning from={hubRemoteOf(d, byId[d.client_id]).at} testid="{testid}-remote-key-warning" />{/if}
					{#if d.remote_access === 'enabled'}<p class="cons warn"><Icon name="globe" size="14" />{t('profiles.preview.remoteOn')}</p>{/if}
					{#if d.remote_access === 'disabled'}<p class="cons"><Icon name="globe" size="14" />{t('profiles.preview.remoteOff')}</p>{/if}
					{#if d.overrides_removed?.length}
						<p class="cons warn" data-testid="{testid}-removed"><Icon name="sliders" size="14" />{t('profiles.preview.overridesRemoved', { count: d.overrides_removed.length, rules: d.overrides_removed.map(label).join(', ') })}</p>
					{/if}
					{#if d.changes?.length}
						<ul class="chg">
							{#each d.changes as c (c.rule)}
								<li class:loose={c.loosens}>
									<span class="rl">{label(c.rule)}</span>
									<span class="vals"><span class="t-muted">{val(c.rule, c.before)}</span> <span aria-hidden="true">→</span><span class="sr-only">{t('profiles.preview.becomes')}</span> <b>{val(c.rule, c.after)}</b>{#if c.after?.expires_at}<span class="t-help" data-testid="{testid}-until-{c.rule}"> · {t('profiles.preview.until', { time: untilText(c.after.expires_at), then: formatValue(c.rule, c.after.then, ctx) })}</span>{/if}</span>
									{#if c.loosens}<span class="badge badge-warn"><Icon name="warn" />{t('profiles.preview.loosens')}</span>{/if}
									{#if c.rule === 'control_path' && c.after?.reason === 'platform_needs_key'}<span class="t-help" data-testid="{testid}-platform-key">{t('profiles.preview.platformKey')}</span>{/if}
								</li>
							{/each}
						</ul>
					{:else if !d.overrides_removed?.length && d.ha_key === 'unchanged' && d.remote_access === 'unchanged'}
						<p class="t-help">{t('profiles.preview.noValueChange')}</p>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
	{#if listed.length}
		<section class="listed" data-testid="{testid}-listed">
			<p class="t-label">{t('profiles.preview.listedTitle')}</p>
			<ul class="devs">
				{#each listed as d (d.client_id)}
					<li class="box dev" data-testid="{testid}-listed-device">
						<b class="block">{d.name}</b>
						{#if hubRemoteOf(d, byId[d.client_id]).now}<RemoteKeyWarning testid="{testid}-remote-key-warning" />{:else if hubRemoteOf(d, byId[d.client_id]).at}<RemoteKeyWarning from={hubRemoteOf(d, byId[d.client_id]).at} testid="{testid}-remote-key-warning" />{/if}
					</li>
				{/each}
			</ul>
		</section>
	{/if}
</div>

<style>
	.pv {
		display: grid;
		gap: 10px;
		min-width: 0;
	}
	.listed {
		display: grid;
		gap: 6px;
	}
	.listed .t-label {
		margin: 0;
	}
	.lead {
		display: flex;
		gap: 8px;
		align-items: center;
		font-weight: 650;
		margin: 0;
	}
	.devs {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 8px;
	}
	.dev {
		display: grid;
		gap: 4px;
	}
	.cons {
		display: flex;
		gap: 6px;
		align-items: flex-start;
		margin: 0;
		color: var(--ink-2);
	}
	.cons.warn {
		color: var(--warn-ink);
	}
	.cons :global(.ic) {
		flex: none;
		margin-top: 3px;
	}
	.chg {
		list-style: none;
		margin: 4px 0 0;
		padding: 0;
		display: grid;
		gap: 4px;
	}
	.chg li {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 10px;
		align-items: baseline;
	}
	.rl {
		min-width: 12em;
		color: var(--ink-2);
	}
	.vals {
		min-width: 0;
		overflow-wrap: anywhere;
	}
</style>
