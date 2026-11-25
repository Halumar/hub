<script>
	/**
	 * Family: open requests of the children ("Can I have more time?") with
	 * Yes (duration) / No (short answer) – the first answer wins, also against
	 * family managers; the requests of the last 24 hours; who looks after
	 * which children; the house settings of the requests. The right itself
	 * is a profile rule (gallery suggestion "Parents"), the children and HA
	 * notify targets are set on the device page.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { onBroadcast } from '$lib/stores/ws-store.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import { debounce, formatTimeRemaining, formatWeekdayTime } from '$lib/utils.js';
	import { L, loadCatalog, ruleOf } from '$lib/profiles/catalog.svelte.js';
	import { errorText } from '$lib/profiles/errors.js';
	import { REQUEST_KINDS } from '$lib/profiles/format.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Avatar from '$lib/ui/Avatar.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';

	// Durations per kind (data/family.json request_categories.presets / default_preset).
	const PRESETS = {
		stay_up: { list: ['15m', '30m', '1h', 'bedtime_end'], default: '30m' },
		unlock_device: { list: ['15m', '30m', '1h', '2h', 'bedtime_end'], default: '30m' },
		end_pause: { list: ['15m', '30m', '1h'], default: '15m' },
	};
	const DECLINE = ['none', 'not_now', 'come_to_me', 'tomorrow'];

	let data = $state(null);
	let loading = $state(true);
	let loadError = $state('');
	let busy = $state('');
	let now = $state(Date.now());
	let choice = $state({});
	let message = $state({});
	let settings = $state(null);
	let savingSettings = $state(false);

	let unsubs = [];
	let clock;
	const refresh = debounce(() => load(), 250);

	onMount(() => {
		load();
		loadCatalog().catch(() => {});
		clock = setInterval(() => (now = Date.now()), 15_000);
		unsubs.push(...['family_changed', 'profiles_changed', 'client_paired', 'client_kicked', 'resync'].map((type) => onBroadcast(type, refresh)));
	});

	onDestroy(() => {
		clearInterval(clock);
		refresh.cancel();
		unsubs.forEach((fn) => fn());
	});

	async function load() {
		try {
			data = await api.getFamily();
			if (!settings) settings = JSON.parse(JSON.stringify(data.settings));
			loadError = '';
		} catch (e) {
			loadError = t('family.loadFailed', { message: errorText(e) });
		} finally {
			loading = false;
		}
	}

	function presetOf(r) {
		return choice[r.request_id] || PRESETS[r.category]?.default || '30m';
	}

	function askText(r) {
		return t(`family.categoryAsk.${r.category}`, { entity: r.entity_name || r.entity_id || '' });
	}

	async function answer(r, yes) {
		busy = r.request_id;
		try {
			const body = yes ? { answer: 'yes', preset: presetOf(r) } : { answer: 'no', ...(message[r.request_id] && message[r.request_id] !== 'none' ? { message: message[r.request_id] } : {}) };
			const res = await api.answerFamilyRequest(r.request_id, body);
			addToast(yes ? t('family.requests.answeredYes', { time: formatWeekdayTime(res.answer?.until) }) : t('family.requests.answered', { status: t('family.status.declined') }), 'success');
		} catch (e) {
			const by = e?.request?.answer?.by?.name;
			addToast(by ? t('family.requests.alreadyAnswered', { name: by }) : t('family.saveFailed', { message: errorText(e) }), 'error');
		} finally {
			busy = '';
			load();
		}
	}

	async function saveSettings() {
		savingSettings = true;
		try {
			settings = await api.saveFamilySettings(settings);
			addToast(t('family.settings.saved'), 'success');
			load();
		} catch (e) {
			addToast(t('family.saveFailed', { message: errorText(e) }), 'error');
		} finally {
			savingSettings = false;
		}
	}

	function toggleCategory(c) {
		const has = settings.categories.includes(c);
		settings.categories = REQUEST_KINDS.filter((x) => (x === c ? !has : settings.categories.includes(x)));
	}

	function childName(c) {
		return c.owner_label || c.name;
	}

	let byId = $derived(Object.fromEntries([...(data?.managers || []).map((m) => [m.client_id, m]), ...(data?.children || []).map((c) => [c.client_id, c])]));
	let secondsLeft = (iso) => Math.max(0, Math.round((Date.parse(iso) - now) / 1000));
</script>

<div class="page">
	<PageHeader title={t('family.title')} subtitle={t('family.subtitle')} />

	{#if loading}
		<LoadingState />
	{:else if loadError && !data}
		<div class="card"><ErrorState message={loadError} onretry={load} /></div>
	{:else}
		<section class="card" aria-labelledby="freq-h" data-testid="family-requests">
			<div class="card-head"><Icon name="bell" /><h2 id="freq-h" class="flex-1">{t('family.requests.title')}</h2>
				{#if data.open_requests.length}<span class="badge badge-warn" data-testid="family-open-count">{data.open_requests.length}</span>{/if}
			</div>
			<p class="t-help mb-3">{t('family.requests.hint', { minutes: data.settings.request_timeout_minutes })}</p>
			{#if !data.open_requests.length}
				<p class="t-help" data-testid="family-requests-empty">{t('family.requests.empty')}</p>
			{:else}
				<div class="rgrid" data-eq="family-requests">
					{#each data.open_requests as r (r.request_id)}
						{@const presets = PRESETS[r.category]?.list || []}
						<article class="rcard" aria-labelledby="r-{r.request_id}" data-testid="family-request">
							<div class="rh">
								<Avatar name={r.child_name} />
								<span class="min-w-0 flex-1">
									<h3 id="r-{r.request_id}" class="t-card truncate">{r.child_name}</h3>
									<small class="t-help block truncate">{t('family.requests.left', { time: formatTimeRemaining(r.expires_at) })}</small>
								</span>
								<span class="badge flex-none">{t(`family.category.${r.category}`)}</span>
							</div>
							<p class="ask" title={askText(r)}>{askText(r)}</p>
							<p class="note t-help" title={r.note || ''}>{r.note ? `„${r.note}“` : ' '}</p>
							<label class="t-label" for="dur-{r.request_id}">{t('family.requests.duration')}</label>
							<select id="dur-{r.request_id}" class="select" value={presetOf(r)} onchange={(e) => (choice[r.request_id] = e.currentTarget.value)} data-testid="family-request-preset">
								{#each presets as p (p)}<option value={p}>{t(`family.preset.${p}`)}</option>{/each}
							</select>
							<label class="t-label" for="msg-{r.request_id}">{t('family.requests.answerText')}</label>
							<select id="msg-{r.request_id}" class="select" value={message[r.request_id] || 'none'} onchange={(e) => (message[r.request_id] = e.currentTarget.value)} data-testid="family-request-message">
								{#each DECLINE as m (m)}<option value={m}>{t(`family.decline.${m}`)}</option>{/each}
							</select>
							<div class="ractions">
								<button type="button" class="btn btn-primary btn-sm" onclick={() => answer(r, true)} disabled={busy === r.request_id || secondsLeft(r.expires_at) === 0 || r.can_approve === false} aria-label={t('family.requests.yesLabel', { preset: t(`family.preset.${presetOf(r)}`), child: r.child_name })} data-testid="family-request-yes"><Icon name="check" />{t('family.requests.yes')}</button>
								<button type="button" class="btn btn-sm" onclick={() => answer(r, false)} disabled={busy === r.request_id || secondsLeft(r.expires_at) === 0} aria-label={t('family.requests.noLabel', { child: r.child_name })} data-testid="family-request-no"><Icon name="x" />{t('family.requests.no')}</button>
							</div>
						</article>
					{/each}
				</div>
			{/if}

			<h3 class="t-label mt-4">{t('family.requests.recentTitle')}</h3>
			{#if data.recent_requests?.length}
				<ul class="recent" data-testid="family-recent">
					{#each data.recent_requests as r (r.request_id)}
						<li class="recent-row" data-testid="family-recent-row" data-status={r.status}>
							<span class="badge flex-none" class:badge-ok={r.status === 'approved'} class:badge-err={r.status === 'declined'}>{t(`family.status.${r.status}`)}</span>
							<span class="min-w-0 flex-1 truncate">{r.child_name} {askText(r)}</span>
							<small class="t-help flex-none">
								{#if r.answer?.by}{t(r.answer.by.kind === 'babysitter' ? 'family.requests.byBabysitter' : 'family.requests.by', { name: r.answer.by.name })} · {/if}{formatWeekdayTime(r.answer?.at || r.expires_at)}
							</small>
						</li>
					{/each}
				</ul>
			{:else}
				<p class="t-help">{t('family.requests.recentEmpty')}</p>
			{/if}
		</section>

		<div class="two">
			<section class="card" aria-labelledby="fman-h" data-testid="family-managers">
				<div class="card-head"><Icon name="users" /><h2 id="fman-h">{t('family.managers.title')}</h2></div>
				<p class="t-help mb-3">{t('family.managers.hint')}</p>
				{#if !data.managers.length}
					<EmptyState icon="users" title={t('family.managers.title')} text={t('family.managers.empty')}>
						<a class="btn btn-sm" href="{base}/profiles"><Icon name="template" />{t('nav.profiles')}</a>
					</EmptyState>
				{:else}
					<ul class="plist">
						{#each data.managers as m (m.client_id)}
							<li class="prow" data-testid="family-manager">
								<Avatar name={m.owner_label || m.name} />
								<span class="min-w-0 flex-1">
									<a class="block truncate" href="{base}/clients/{m.client_id}">{m.owner_label ? `${m.owner_label} · ${m.name}` : m.name}</a>
									<small class="t-help block truncate">
										{m.scope.all ? t('family.managers.allChildren') : t('family.managers.children', { count: m.child_ids.length })}
										· {m.remote === 'away' ? t('family.managers.away') : t('family.managers.homeOnly')}
										· {m.notify_targets?.length ? t('family.managers.targets', { count: m.notify_targets.length }) : t('family.managers.noTargets')}
									</small>
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="card" aria-labelledby="fkid-h" data-testid="family-children">
				<div class="card-head"><Icon name="child" /><h2 id="fkid-h">{t('family.children.title')}</h2></div>
				<p class="t-help mb-3">{t('family.children.hint')}</p>
				{#if !data.children.length}
					<p class="t-help">{t('family.children.empty')}</p>
				{:else}
					<ul class="plist">
						{#each data.children as c (c.client_id)}
							<li class="prow" data-testid="family-child">
								<Avatar name={childName(c)} />
								<span class="min-w-0 flex-1">
									<a class="block truncate" href="{base}/clients/{c.client_id}">{c.owner_label ? `${c.owner_label} · ${c.name}` : c.name}</a>
									<small class="t-help block truncate">
										{c.manager_ids?.length ? t('family.children.managedBy', { names: c.manager_ids.map((id) => byId[id]?.owner_label || byId[id]?.name || '').join(', ') }) : t('family.children.nobody')}
									</small>
									{#if c.fixed_manageable?.length}
										<small class="t-help block truncate">{t('family.children.fixed', { rules: c.fixed_manageable.map((k) => L(ruleOf(k)?.label) || k).join(', ') })}</small>
									{/if}
								</span>
								<span class="badge flex-none truncate">{c.profile_name}</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>

		{#if settings}
			<section class="card mt-4" aria-labelledby="fset-h" data-testid="family-settings">
				<div class="card-head"><Icon name="gear" /><h2 id="fset-h">{t('family.settings.title')}</h2></div>
				<Switch bind:checked={settings.requests_enabled} label={t('family.settings.requestsEnabled')} description={t('family.settings.requestsEnabledHint')} testid="family-requests-enabled" />
				<span class="t-label mt-3 block" id="fset-cat">{t('family.settings.categories')}</span>
				<div class="cats" role="group" aria-labelledby="fset-cat">
					{#each REQUEST_KINDS as c (c)}
						<label class="check"><input type="checkbox" checked={settings.categories.includes(c)} onchange={() => toggleCategory(c)} disabled={!settings.requests_enabled} /><span class="truncate">{t(`family.category.${c}`)}</span></label>
					{/each}
				</div>
				<div class="nums">
					<label class="nfield"><span class="t-label">{t('family.settings.timeout')}</span><input type="number" class="input" min="2" max="30" bind:value={settings.request_timeout_minutes} data-testid="family-timeout" /></label>
					<label class="nfield"><span class="t-label">{t('family.settings.perHour')}</span><input type="number" class="input" min="1" max="20" bind:value={settings.child_requests_per_hour} /></label>
					<label class="nfield"><span class="t-label">{t('family.settings.cooldown')}</span><input type="number" class="input" min="0" max="120" bind:value={settings.decline_cooldown_minutes} /></label>
					<label class="nfield"><span class="t-label">{t('family.settings.activityDays')}</span><input type="number" class="input" min="1" max="30" bind:value={settings.activity_days} /></label>
				</div>
				<div class="mt-3"><button type="button" class="btn btn-primary btn-sm" onclick={saveSettings} disabled={savingSettings} data-testid="family-settings-save">{t('family.settings.save')}</button></div>
			</section>
		{/if}
	{/if}
</div>

<style>
	/* Equal sizes: request cards of a row are equal: name 1 line, the question 2
	   reserved lines, the note 1 reserved line, equal buttons. */
	.rgrid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
		gap: 12px;
	}
	.rcard {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		grid-template-rows: auto calc(2 * 1.4em) 1.4em auto auto auto auto auto;
		gap: 6px;
		padding: 12px;
		border: 1px solid var(--line);
		border-radius: 12px;
		min-width: 0;
		height: 100%;
	}
	.rh {
		display: flex;
		gap: 10px;
		align-items: center;
		min-width: 0;
	}
	.ask {
		margin: 0;
		line-height: 1.4em;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.note {
		margin: 0;
		line-height: 1.4em;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.ractions {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
		margin-top: 4px;
	}
	.ractions .btn {
		justify-content: center;
		min-width: 0;
	}
	.recent,
	.plist {
		display: grid;
		gap: 8px;
		margin: 6px 0 0;
		padding: 0;
		list-style: none;
	}
	.recent-row,
	.prow {
		display: flex;
		gap: 10px;
		align-items: center;
		min-width: 0;
	}
	.two {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 14px;
		margin-top: 14px;
	}
	.cats {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 6px 14px;
		margin-top: 6px;
	}
	.check {
		display: flex;
		gap: 8px;
		align-items: center;
		min-width: 0;
	}
	.nums {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 10px;
		margin-top: 12px;
	}
	.nfield {
		display: grid;
		gap: 4px;
		min-width: 0;
	}
	@media (max-width: 860px) {
		.nums,
		.cats {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}
	@media (max-width: 767px) {
		.two {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	@media (max-width: 420px) {
		.nums,
		.cats {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
