<script>
	/**
	 * Notifications at events, sub-page of the log tab: templates,
	 * events in groups (switch + summary + edit panel), targets (Home Assistant
	 * notify.* services and member devices), quiet hours, bundling, "Send
	 * test" and the HA event <ns>_event for own automations.
	 */
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { api } from '$lib/api.js';
	import { t, i18n } from '$lib/i18n/index.svelte.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import { platformIcon } from '$lib/device-info.js';
	import Modal from '$lib/ui/Modal.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import ErrorState from '$lib/ui/ErrorState.svelte';
	import LogHeader from '$lib/components/log/LogHeader.svelte';
	import { BRAND } from '$lib/brand.ts';
	import { locale, setNamespace } from '$lib/log/describe.js';
	import { GROUPS, ROWS, ROW, TEMPLATES, ruleFromRow, rowsFromRules, rulesFromRows } from '$lib/log/rows.js';

	const HM = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
	const SAFETY_DOMAINS = ['lock', 'cover', 'alarm_control_panel', 'valve', 'siren', 'climate'];

	let summary = $state(null);
	let config = $state(null);
	let rows = $state({});
	let targets = $state({ notify: [], devices: [], error: null });
	let entities = $state([]);
	let loading = $state(true);
	let error = $state('');
	let quietFrom = $state('22:00');
	let quietTo = $state('07:00');
	let quietError = $state(false);

	// edit panel
	let editOpen = $state(false);
	let editKey = $state('');
	let draft = $state(null);
	let tested = $state(null);
	let testing = $state(false);
	let saving = $state(false);

	// template preview
	let tplOpen = $state(false);
	let tplKey = $state('');

	let ns = $derived(summary?.namespace || '');
	let facets = $derived(summary?.facets || { devices: [], owners: [], profiles: [], areas: [] });

	onMount(async () => {
		try {
			const [s, c] = await Promise.all([api.getLogSummary(), api.getEventNotifications()]);
			summary = s;
			setNamespace(s?.namespace);
			config = c;
			rows = rowsFromRules(c.rules);
			quietFrom = c.quiet_default?.start || '22:00';
			quietTo = c.quiet_default?.end || '07:00';
		} catch (e) {
			error = t('lognotify.loadFailed', { message: e.message });
		} finally {
			loading = false;
		}
		loadTargets();
		api.getHAEntities()
			.then((list) => (entities = (list || []).filter((e) => SAFETY_DOMAINS.includes(e.domain)).sort((a, b) => a.friendly_name.localeCompare(b.friendly_name)).slice(0, 24)))
			.catch(() => (entities = []));
	});

	async function loadTargets(toast = false) {
		try {
			targets = await api.getNotifyTargets();
			if (toast) addToast(t('lognotify.reloaded', { count: targets.notify.length }), 'success');
		} catch (e) {
			targets = { notify: [], devices: [], error: 'ha_unavailable' };
		}
	}

	async function persist(next) {
		saving = true;
		try {
			const quiet = { start: quietFrom, end: quietTo };
			const body = { retention_days: config.retention_days, quiet_default: quiet, rules: rulesFromRows(next, quiet, config.rules) };
			config = await api.saveEventNotifications(body);
			rows = rowsFromRules(config.rules);
			return true;
		} catch (e) {
			addToast(t('log.saveFailed', { message: e.message }), 'error');
			return false;
		} finally {
			saving = false;
		}
	}

	const label = (key) => t(`lognotify.rows.${key}`);
	const hasTarget = (s) => s.notify.length + s.devices.length > 0;

	async function toggleRow(key) {
		const s = rows[key];
		if (!s.on && !hasTarget(s)) {
			const first = defaultTargets();
			if (!first.notify.length && !first.devices.length) {
				openEdit(key, true);
				return;
			}
			s.notify = first.notify;
			s.devices = first.devices;
		}
		const next = structuredClone($state.snapshot(rows));
		next[key].on = !s.on;
		if (await persist(next)) addToast(t('lognotify.toggled', { name: label(key), state: next[key].on ? t('lognotify.on') : t('lognotify.offLower') }), 'success');
	}

	/** Targets already in use, else the first Companion app service. */
	function defaultTargets() {
		const used = { notify: new Set(), devices: new Set() };
		for (const s of Object.values(rows)) {
			if (!s.on) continue;
			s.notify.forEach((x) => used.notify.add(x));
			s.devices.forEach((x) => used.devices.add(x));
		}
		const available = new Set(targets.notify.map((n) => n.service));
		let notify = [...used.notify].filter((x) => available.has(x) || !targets.notify.length);
		const devices = [...used.devices];
		if (!notify.length && !devices.length) {
			const mobile = targets.notify.find((n) => n.service.startsWith('notify.mobile_app_')) || targets.notify.find((n) => n.service !== 'notify.notify');
			if (mobile) notify = [mobile.service];
		}
		return { notify, devices };
	}

	function openEdit(key, turnOn = false) {
		editKey = key;
		draft = structuredClone($state.snapshot(rows[key]));
		if (turnOn) draft.on = true;
		tested = null;
		editOpen = true;
	}

	async function saveEdit() {
		const next = structuredClone($state.snapshot(rows));
		next[editKey] = structuredClone($state.snapshot(draft));
		if (await persist(next)) {
			addToast(t('lognotify.savedRow', { name: label(editKey) }), 'success');
			editOpen = false;
		}
	}

	async function sendTest() {
		testing = true;
		try {
			const rule = ruleFromRow(ROW[editKey], { ...$state.snapshot(draft), on: true }, { start: quietFrom, end: quietTo });
			const r = await api.testEventNotification({ rule });
			tested = { at: new Date(), ...r };
			addToast(t('lognotify.testSentToast', { count: r.results.length }), 'success');
		} catch (e) {
			addToast(t('log.saveFailed', { message: e.message }), 'error');
		} finally {
			testing = false;
		}
	}

	function toggleIn(list, v) {
		return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
	}

	// templates
	function tplTargets() {
		return defaultTargets();
	}
	async function applyTemplate() {
		const tpl = TEMPLATES.find((x) => x.key === tplKey);
		const to = tplTargets();
		const next = structuredClone($state.snapshot(rows));
		for (const key of tpl.rows) {
			const s = next[key];
			const wasOn = s.on;
			s.on = true;
			s.notify = [...new Set([...(wasOn ? s.notify : []), ...to.notify])];
			s.devices = [...new Set([...(wasOn ? s.devices : []), ...to.devices])];
			if (tpl.quiet && !wasOn) s.quiet = tpl.quiet;
			if (tpl.bundle?.[key]) s.bundle = tpl.bundle[key];
			if (key === 'act_entities' && tpl.entities && !s.ents.length) s.ents = entities.filter((e) => ['lock', 'cover'].includes(e.domain)).slice(0, 6).map((e) => e.entity_id);
		}
		if (await persist(next)) {
			addToast(t('lognotify.tplApplied', { name: t(`lognotify.tpl.${tpl.key}`), count: tpl.rows.length }), 'success');
			tplOpen = false;
		}
	}

	async function saveQuiet() {
		quietError = !HM.test(quietFrom) || !HM.test(quietTo);
		if (quietError) return;
		if (await persist(structuredClone($state.snapshot(rows)))) addToast(t('log.saved'), 'success');
	}

	function deviceHint(d) {
		if (d.kind === 'child') return t('lognotify.childHint');
		if (d.kind === 'guest') return t('lognotify.guestHint', { date: d.expires_at ? new Date(d.expires_at).toLocaleString(locale(), { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '–' });
		return d.owner_label || '';
	}

	function targetName(id) {
		if (id.startsWith('notify.')) return id;
		return targets.devices.find((d) => d.client_id === id)?.name || id;
	}
	function filterText(k, v) {
		if (k === 'device') return facets.devices.find((d) => d.id === v)?.name || v;
		if (k === 'profile') return facets.profiles.find((p) => p.id === v)?.name || v;
		if (k === 'area') return facets.areas.find((a) => a.id === v)?.name || v;
		return v;
	}
	function filterOptions(k) {
		if (k === 'person') return facets.owners.map((o) => [o, o]);
		if (k === 'device') return facets.devices.map((d) => [d.id, d.name]);
		if (k === 'profile') return facets.profiles.map((p) => [p.id, p.name]);
		return facets.areas.map((a) => [a.id, a.name]);
	}
	function summaryParts(row, s) {
		if (!s.on) return [{ icon: null, text: t('lognotify.off') }];
		const to = [...s.notify, ...s.devices];
		const parts = [to.length ? { icon: 'send', text: to.slice(0, 2).map(targetName).join(', ') + (to.length > 2 ? ` +${to.length - 2}` : '') } : { icon: 'warn', text: t('lognotify.noTarget'), strong: true }];
		const fs = Object.entries(s.f).filter(([, v]) => v);
		if (fs.length) parts.push({ icon: 'sliders', text: fs.map(([k, v]) => filterText(k, v)).join(', ') });
		if (row.entities && s.ents.length) parts.push({ icon: 'home', text: s.ents.map((e) => entities.find((x) => x.entity_id === e)?.friendly_name || e).join(', ') });
		if (row.thr && s.thr) parts.push({ icon: null, text: t(`lognotify.thr.${s.thr}`) });
		if (s.quiet === 'night') parts.push({ icon: 'moon', text: t('lognotify.quietShort', { start: quietFrom, end: quietTo }) });
		if (s.bundle) parts.push({ icon: 'list', text: t('lognotify.bundled', { label: s.bundle === 10 ? t('lognotify.bundle10Short') : t('lognotify.bundle60Short') }) });
		return parts;
	}

	let onCount = $derived(Object.values(rows).filter((s) => s.on).length);
	let exampleYaml = $derived(`trigger:
  - trigger: event
    event_type: ${ns}_event
    event_data:
      kind: kids_bedtime_attempt   # ${t('lognotify.exampleComment')} denied, guest_expiring, control
action:
  - action: light.turn_on
    target: { entity_id: light.hallway }
    data: { flash: long }
# ${t('lognotify.exampleMore')}: category, client_id, device_name, owner_label, profile_id, entity_id, route, reason, count`);

	async function copyExample() {
		try {
			await navigator.clipboard.writeText(exampleYaml);
			addToast(t('lognotify.copied'), 'success');
		} catch {
			/* clipboard blocked */
		}
	}

	let row = $derived(editKey ? ROW[editKey] : null);
	let draftHa = $derived(draft ? draft.notify.length : 0);
	let draftTargets = $derived(draft ? draft.notify.length + draft.devices.length : 0);
	let tpl = $derived(TEMPLATES.find((x) => x.key === tplKey));
</script>

<svelte:head><title>{t('log.tabNotify')} · {t('log.title')}</title></svelte:head>

<div class="page logpage" data-testid="lognotify-page" lang={i18n.locale}>
	<LogHeader active="notify" {summary} />
	{#if loading}
		<LoadingState />
	{:else if error}
		<ErrorState message={error} />
	{:else}
		<div class="stack">
			<p class="t-sub">{t('lognotify.intro')} <span class="t-muted">{t('lognotify.onCount', { on: onCount, total: ROWS.length })}</span></p>
			<div class="banner banner-neutral" role="note">
				<Icon name="info" />
				<div class="banner-body"><b>{t('lognotify.notAlarmTitle')}</b> {t('lognotify.notAlarmText')}</div>
			</div>

			<section class="stack-sm" aria-labelledby="h-tpl">
				<div>
					<h2 id="h-tpl" class="t-section">{t('lognotify.templates')}</h2>
					<p class="t-sub">{t('lognotify.templatesText')}</p>
				</div>
				<div class="tiles c4 tpl" data-eq="templates">
					{#each TEMPLATES as tp (tp.key)}
						<button type="button" class="tile" onclick={() => ((tplKey = tp.key), (tplOpen = true))} data-testid="tpl-{tp.key}">
							<span class="tile-row">
								<span class="chipic sm {tp.chip}"><Icon name={tp.icon} /></span><span class="grow"></span>
								{#if tp.rows.every((k) => rows[k]?.on)}<span class="badge badge-ok"><Icon name="check" />{t('lognotify.active')}</span>{/if}
							</span>
							<b>{t(`lognotify.tpl.${tp.key}`)}</b>
							<small>{t(`lognotify.tpl.${tp.key}Sub`)}</small>
						</button>
					{/each}
				</div>
			</section>

			{#each GROUPS as g (g.key)}
				{@const list = ROWS.filter((r) => r.group === g.key)}
				{@const n = list.filter((r) => rows[r.key].on).length}
				<section class="card evgroup" aria-labelledby="eg-{g.key}">
					<div class="group-head">
						<span class="chipic sm"><Icon name={g.icon} /></span>
						<h2 id="eg-{g.key}">{t(`lognotify.groups.${g.key}`)}</h2>
						<span class="badge" class:badge-ok={n > 0}>{t('lognotify.groupOn', { on: n, total: list.length })}</span>
						{#if g.help}<p class="t-help">{t(`lognotify.groups.${g.key}Help`)}</p>{/if}
					</div>
					<ul class="evlist">
						{#each list as r (r.key)}
							{@const s = rows[r.key]}
							<li class="evrow" class:on={s.on} data-testid="evrow-{r.key}">
								<button type="button" class="switch" role="switch" aria-checked={s.on ? 'true' : 'false'} aria-label={t('lognotify.notifyLabel', { name: label(r.key) })} onclick={() => toggleRow(r.key)} disabled={saving} data-testid="evswitch-{r.key}"></button>
								<div class="evmain">
									<b>{label(r.key)}</b>
									<span class="evsum">
										{#each summaryParts(r, s) as p, i (i)}
											<span>{#if p.icon}<Icon name={p.icon} />{/if}{#if p.strong}<b>{p.text}</b>{:else}{p.text}{/if}</span>
										{/each}
									</span>
								</div>
								<button type="button" class="btn btn-sm" aria-label={t('lognotify.editLabel', { name: label(r.key) })} onclick={() => openEdit(r.key)} data-testid="evedit-{r.key}"><Icon name="edit" />{t('lognotify.edit')}</button>
							</li>
						{/each}
					</ul>
				</section>
			{/each}

			<section class="card stack-sm" aria-labelledby="h-tg">
				<div class="card-head"><Icon name="send" /><h2 id="h-tg">{t('lognotify.targets')}</h2></div>
				<div class="two">
					<div class="box stack-sm">
						<b><Icon name="bell" /> {t('lognotify.targetsHa')}</b>
						<span class="t-help">{@html t('lognotify.targetsHaText')}</span>
						<span class="t-help" data-testid="notify-services">
							{#if targets.error}{t('lognotify.servicesError')}{:else}
								{t('lognotify.targetsFound', { count: targets.notify.length })}
								{#each targets.notify as n (n.service)} <code>{n.service}</code>{/each}
							{/if}
						</span>
					</div>
					<div class="box stack-sm">
						<b><Icon name="iphone" /> {t('lognotify.targetsDev')}</b>
						<span class="t-help">{@html t('lognotify.targetsDevText')}</span>
						<span class="t-help">{t('lognotify.namesHint')}</span>
					</div>
				</div>
				<p class="t-help">{t('lognotify.targetsHaDown')}</p>
				<div class="btnrow" style="width:min(100%,240px)"><button type="button" class="btn btn-sm" onclick={() => loadTargets(true)} data-testid="notify-reload"><Icon name="refresh" />{t('lognotify.reload')}</button></div>
			</section>

			<section class="card stack-sm" aria-labelledby="h-q">
				<div class="card-head"><Icon name="moon" /><h2 id="h-q">{t('lognotify.quietTitle')}</h2></div>
				<div class="qrow">
					<label class="field"><span class="field-label">{t('lognotify.from')}</span><input class="input" type="text" inputmode="numeric" maxlength="5" bind:value={quietFrom} onchange={saveQuiet} aria-invalid={quietError} data-testid="quiet-from" /></label>
					<label class="field"><span class="field-label">{t('lognotify.to')}</span><input class="input" type="text" inputmode="numeric" maxlength="5" bind:value={quietTo} onchange={saveQuiet} aria-invalid={quietError} data-testid="quiet-to" /></label>
				</div>
				{#if quietError}<span class="field-error" role="alert">{t('lognotify.quietInvalid')}</span>{/if}
				<span class="t-help">{t('lognotify.quietText', { start: quietFrom, end: quietTo })}</span>
			</section>

			<section class="card stack-sm" aria-labelledby="h-ev">
				<div class="card-head"><Icon name="terminal" /><h2 id="h-ev">{t('lognotify.automations')}</h2></div>
				<p class="t-sub">{t('lognotify.automationsText', { event: `${ns}_event` })}</p>
				<!-- svelte-ignore a11y_no_noninteractive_tabindex (scrollable code must be reachable by keyboard) -->
				<pre class="code" tabindex="0" aria-label={t('lognotify.example')}>{exampleYaml}</pre>
				<div class="btnrow" style="width:min(100%,240px)"><button type="button" class="btn btn-sm" onclick={copyExample}><Icon name="copy" />{t('lognotify.copy')}</button></div>
				<p class="t-help"><a class="link" href="{base}/notifications">{t('lognotify.messagesLink')}</a></p>
			</section>
		</div>
	{/if}

	{#if draft && row}
		<Modal bind:open={editOpen} title={label(editKey)} description={t(`lognotify.groups.${row.group}`)} size="lg" testid="ev-panel">
			<div class="stack">
				<div class="evtop">
					<div class="grow">
						<b id="evon-l">{t('lognotify.notify')}</b>
						<p class="t-help">{row.help ? t(`lognotify.rows.${editKey}Help`) : t('lognotify.sendHelp')}</p>
					</div>
					<button type="button" class="switch" role="switch" aria-checked={draft.on ? 'true' : 'false'} aria-labelledby="evon-l" onclick={() => (draft.on = !draft.on)} data-testid="evd-on"></button>
				</div>
				<div class="field">
					<span class="field-label">{t('lognotify.sample')}</span>
					<div class="notif-sample"><Icon name="bell" /><div><b>{BRAND.name}</b><span>{draft.bundle && row.bundleExample ? t(`lognotify.examples.${editKey}Bundle`) : t(`lognotify.examples.${editKey}`)}</span></div></div>
				</div>
				{#if draft.on}
					{#if row.thr}
						<div class="field">
							<span class="field-label" id="lab-thr">{row.timing ? t('lognotify.timing') : t('lognotify.threshold')}</span>
							<div class="seg even full" role="group" aria-labelledby="lab-thr" data-eq="thr">
								{#each row.thr as [k] (k)}<button type="button" aria-pressed={draft.thr === k} onclick={() => (draft.thr = k)}>{t(`lognotify.thr.${k}`)}</button>{/each}
							</div>
							{#if t(`lognotify.thrHelp.${editKey}`) !== `lognotify.thrHelp.${editKey}`}<span class="t-help">{t(`lognotify.thrHelp.${editKey}`)}</span>{/if}
						</div>
					{/if}
					{#if row.entities}
						<div class="field">
							<span class="field-label" id="ents-l">{t('lognotify.whichDevices')}</span>
							{#if entities.length}
								<div class="tiles c3" role="group" aria-labelledby="ents-l" data-eq="ents">
									{#each entities as e (e.entity_id)}
										<button type="button" class="tile ent" role="checkbox" aria-checked={draft.ents.includes(e.entity_id)} onclick={() => (draft.ents = toggleIn(draft.ents, e.entity_id))}>
											<b>{e.friendly_name}</b><small class="mono">{e.entity_id}</small>
										</button>
									{/each}
								</div>
							{:else}<p class="t-help">{t('lognotify.whichDevicesNone')}</p>{/if}
						</div>
					{/if}
					<div class="field">
						<span class="field-label"><Icon name="bell" /> {t('lognotify.haServices')}</span>
						{#if targets.error}
							<div class="box">{t('lognotify.servicesError')}</div>
						{:else if !targets.notify.length}
							<div class="box"><Icon name="info" /> {t('lognotify.noServices')}</div>
						{:else}
							<div class="tiles tgt" role="group" aria-label={t('lognotify.haServices')} data-eq="tgt-ha">
								{#each targets.notify as n (n.service)}
									<button type="button" class="tile" role="checkbox" aria-checked={draft.notify.includes(n.service)} onclick={() => ((draft.notify = toggleIn(draft.notify, n.service)), (tested = null))} data-testid="tgt-{n.service}">
										<span class="tile-row"><Icon name="bell" /><span class="grow"></span><span class="tick" aria-hidden="true">{#if draft.notify.includes(n.service)}<Icon name="checkcircle" />{/if}</span></span>
										<b class="mono">{n.service}</b><small>{n.name}</small>
									</button>
								{/each}
							</div>
						{/if}
						<span class="t-help">{t('lognotify.haServicesHelp')}</span>
					</div>
					<div class="field">
						<span class="field-label"><Icon name="iphone" /> {t('lognotify.devices')}</span>
						{#if targets.devices.length}
							<div class="tiles tgt" role="group" aria-label={t('lognotify.devices')} data-eq="tgt-dev">
								{#each targets.devices as d (d.client_id)}
									<button type="button" class="tile" role="checkbox" aria-checked={draft.devices.includes(d.client_id)} onclick={() => ((draft.devices = toggleIn(draft.devices, d.client_id)), (tested = null))} data-testid="tgt-dev-{d.client_id}">
										<span class="tile-row"><Icon name={d.kind === 'child' ? 'child' : d.kind === 'guest' ? 'users' : platformIcon(d.platform)} /><span class="grow"></span><span class="tick" aria-hidden="true">{#if draft.devices.includes(d.client_id)}<Icon name="checkcircle" />{/if}</span></span>
										<b>{d.name}</b><small data-testid="tgt-hint-{d.client_id}">{deviceHint(d)}</small>
									</button>
								{/each}
							</div>
						{:else}<div class="box">{t('lognotify.devicesNone')}</div>{/if}
						<span class="t-help">{t('lognotify.devicesHelp')}</span>
						{#if draft.devices.length}<span class="t-help" data-testid="names-hint"><Icon name="info" /> {t('lognotify.namesHint')}</span>{/if}
					</div>
					{#if !draftTargets}
						<div class="banner banner-warn" data-testid="ev-no-target"><Icon name="warn" /><div class="banner-body">{t('lognotify.statusNoTarget', { ns })}</div></div>
					{:else if !draftHa}
						<div class="banner banner-neutral"><Icon name="info" /><div class="banner-body">{t('lognotify.statusOnlyDevices')}</div></div>
					{/if}
					{#if row.f.length}
						<div class="field">
							<span class="field-label">{t('lognotify.onlyFor')}</span>
							<div class="lsel c{row.f.length}" data-eq="evf">
								{#each row.f as k (k)}
									<label class="field">
										<span class="t-help">{t(`log.${k}`)}</span>
										<select class="select" bind:value={draft.f[k]}>
											<option value="">{t('log.all')}</option>
											{#each filterOptions(k) as [v, text] (v)}<option value={v}>{text}</option>{/each}
										</select>
									</label>
								{/each}
							</div>
						</div>
					{/if}
					<div class="field">
						<span class="field-label" id="lab-quiet">{t('lognotify.quiet')}</span>
						<div class="seg even full" role="group" aria-labelledby="lab-quiet" data-eq="quiet">
							<button type="button" aria-pressed={draft.quiet === 'always'} onclick={() => (draft.quiet = 'always')}>{t('lognotify.quietAlways')}</button>
							<button type="button" aria-pressed={draft.quiet === 'night'} onclick={() => (draft.quiet = 'night')}>{t('lognotify.quietNight', { start: quietFrom, end: quietTo })}</button>
						</div>
						{#if row.group === 'alarms' && draft.quiet === 'night'}<span class="t-help"><Icon name="warn" /> {t('lognotify.quietAlarmWarn')}</span>{/if}
					</div>
					<div class="field">
						<span class="field-label" id="lab-bundle">{t('lognotify.bundle')}</span>
						<div class="seg even full" role="group" aria-labelledby="lab-bundle" data-eq="bundle">
							{#each [[0, 'bundleSingle'], [10, 'bundle10'], [60, 'bundle60']] as [v, k] (v)}
								<button type="button" aria-pressed={draft.bundle === v} onclick={() => (draft.bundle = v)}>{t(`lognotify.${k}`)}</button>
							{/each}
						</div>
						<span class="t-help">{draft.bundle ? t('lognotify.bundleHelp', { example: t('lognotify.examples.kids_controlBundle') }) : t('lognotify.bundleHelpSingle')}</span>
					</div>
					{#if row.back}
						<label class="check"><input type="checkbox" bind:checked={draft.back} /><span>{t('lognotify.back')}</span></label>
					{/if}
					<div class="btnrow" style="width:min(100%,220px)">
						<button type="button" class="btn btn-sm" onclick={sendTest} disabled={!draftTargets || testing} data-testid="ev-test"><Icon name="send" />{t('lognotify.test')}</button>
					</div>
					{#if tested}
						<div class="box stack-sm" role="status" data-testid="ev-test-result">
							<b>{t('lognotify.testSent')} · {tested.at.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' })}</b>
							<ul class="diff">
								{#each tested.results as r, i (i)}
									<li><Icon name={r.status === 'failed' ? 'x' : r.status === 'waiting_for_app' ? 'clock' : 'check'} /><span><b class:mono={r.target.startsWith('notify.')}>{r.name || targetName(r.target)}</b>: {t(`log.sentStatus.${r.status}`)}</span></li>
								{/each}
								<li><Icon name="link" /><span>{tested.ha_event === 'handed_over' ? t('lognotify.testHaEvent', { ns }) : t('lognotify.testHaEventOff', { ns })}</span></li>
							</ul>
							<span class="t-help">{t('lognotify.testHelp')}</span>
						</div>
					{/if}
				{:else}
					<p class="t-help">{t('lognotify.offHelp', { ns })}</p>
				{/if}
				<p class="t-help"><Icon name="terminal" /> {t('lognotify.haEvent')} <code>{ns}_event</code> · <code>kind: {row.kinds[0]}</code></p>
			</div>
			{#snippet footer()}
				<div class="foot-eq" data-eq="evfoot">
					<button type="button" class="btn" onclick={() => (editOpen = false)}>{t('lognotify.cancel')}</button>
					<button type="button" class="btn btn-primary" onclick={saveEdit} disabled={saving || (draft.on && !draftTargets)} data-testid="ev-save">{t('lognotify.save')}</button>
				</div>
			{/snippet}
		</Modal>
	{/if}

	{#if tpl}
		{@const to = tplTargets()}
		<Modal bind:open={tplOpen} title={t('lognotify.tplTitle', { name: t(`lognotify.tpl.${tpl.key}`) })} description={t(`lognotify.tpl.${tpl.key}Sub`)} testid="tpl-panel">
			<div class="stack">
				<div class="stack-sm">
					<h3 class="t-label">{t('lognotify.tplOn')}</h3>
					<ul class="diff">
						{#each tpl.rows as k (k)}
							<li><Icon name={rows[k]?.on ? 'check' : 'plus'} /><span><b>{label(k)}</b>{#if rows[k]?.on} <span class="t-muted">· {t('lognotify.tplAlreadyOn')}</span>{/if}</span></li>
						{/each}
					</ul>
				</div>
				<div class="stack-sm">
					<h3 class="t-label">{t('lognotify.tplTo')}</h3>
					<ul class="diff">
						{#each [...to.notify, ...to.devices] as x (x)}<li><Icon name={x.startsWith('notify.') ? 'bell' : 'iphone'} /><span class:mono={x.startsWith('notify.')}>{targetName(x)}</span></li>{/each}
						{#if !to.notify.length && !to.devices.length}<li><Icon name="warn" /><span>{t('lognotify.tplNoTargets')}</span></li>{/if}
					</ul>
				</div>
				<p class="t-help"><Icon name="info" /> {t('lognotify.tplKeep')}</p>
			</div>
			{#snippet footer()}
				<div class="foot-eq" data-eq="tplfoot">
					<button type="button" class="btn" onclick={() => (tplOpen = false)}>{t('lognotify.cancel')}</button>
					<button type="button" class="btn btn-primary" onclick={applyTemplate} disabled={saving || (!to.notify.length && !to.devices.length)} data-testid="tpl-apply">{t('lognotify.tplApply')}</button>
				</div>
			{/snippet}
		</Modal>
	{/if}
</div>

<style>
	.group-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		margin-bottom: var(--s2);
	}
	.group-head h2 {
		font-size: var(--fs-t3, 15px);
		font-weight: 650;
		margin: 0;
		flex: 1;
	}
	.group-head p {
		flex-basis: 100%;
		margin: 0;
	}
	.evlist {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
	}
	.evrow {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) 8.5rem;
		gap: 12px;
		align-items: center;
		padding: 10px 0;
		border-top: 1px solid var(--line);
	}
	.evrow:first-child {
		border-top: 0;
	}
	.evrow .btn {
		justify-content: center;
		min-width: 0;
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
	}
	.evmain {
		display: grid;
		gap: 2px;
		min-width: 0;
	}
	.evsum {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 12px;
		font-size: var(--fs-cap);
		color: var(--ink-3);
	}
	.evsum > span {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.evsum :global(.ic) {
		width: 12px;
		height: 12px;
		flex: none;
	}
	.two {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--s3);
	}
	.qrow {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 9rem));
		gap: var(--s2);
	}
	.evtop {
		display: flex;
		gap: var(--s3);
		align-items: flex-start;
	}
	.evtop .grow {
		flex: 1;
	}
	.evtop p {
		margin: 2px 0 0;
	}
	.notif-sample {
		display: flex;
		gap: 10px;
		align-items: flex-start;
		padding: 10px 12px;
		border-radius: 14px;
		background: var(--surface-2);
		font-size: var(--fs-sm);
	}
	.notif-sample :global(.ic) {
		width: 18px;
		height: 18px;
		flex: none;
		color: var(--ink-2);
		margin-top: 2px;
	}
	.notif-sample div {
		display: grid;
	}
	.lsel {
		display: grid;
		gap: var(--s2);
		grid-template-columns: repeat(var(--n, 2), minmax(0, 1fr));
	}
	.lsel.c1 {
		--n: 1;
	}
	.lsel.c3 {
		--n: 3;
	}
	.lsel.c4 {
		--n: 4;
	}
	.lsel .select {
		width: 100%;
		min-width: 0;
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
	}
	.tile.ent {
		min-height: 72px;
	}
	@media (max-width: 639px) {
		.evrow {
			grid-template-columns: auto minmax(0, 1fr);
		}
		.evrow .btn {
			grid-column: 2;
			justify-self: start;
			width: 8.5rem;
		}
		.two {
			grid-template-columns: 1fr;
		}
		.lsel,
		.lsel.c3,
		.lsel.c4 {
			--n: 2;
		}
	}
</style>
