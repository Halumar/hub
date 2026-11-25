<script>
	/**
	 * Editor of one profile rule value, chosen by the rule key/type of
	 * the catalog. Used in the profile editor, the device "Adjust" dialog and
	 * the bulk dialog. `value` is bindable; every change calls onchange(v).
	 *
	 * ctx: { areas: [{ id, name }], tags: string[], entities: [{ entity_id, friendly_name }],
	 *        childProtection: bool, ownAreaName?: string (device page),
	 *        hiddenChoices?: { [key]: values not offered (the current value stays visible) } }
	 */
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import QuietHoursEditor from '$lib/components/kids/QuietHoursEditor.svelte';
	import RemoteControlEditor from './RemoteControlEditor.svelte';
	import KidsExceptionsEditor from './KidsExceptionsEditor.svelte';
	import { t } from '$lib/i18n/index.svelte.js';
	import { L } from '$lib/profiles/catalog.svelte.js';
	import { FALLBACK_TYPES, rateAllValid, rateValid, summarize, typeLabel } from '$lib/data/kids.js';
	import { base } from '$app/paths';
	import { ANNOUNCE_VOLUMES, HOME_HEADER_DEFAULT, HOME_PARTS, HOME_STYLES, NIGHT_BRIGHTNESS, PANEL_BRIGHTNESS_MODES, PANEL_LEVELS, PANEL_SENSORS, PANEL_WAKE_DOMAINS, PANEL_MINUTES, POWER_DOMAINS, REQUEST_KINDS, safetyTypes, clone, enumChoices, enumLabel, formatValue, profileKinds, kindLabel, kindIcon } from '$lib/profiles/format.js';

	let { rule, value = $bindable(), ctx = {}, idPrefix = 'rv', disabled = false, onchange = null, label = '' } = $props();

	const key = $derived(rule?.key);
	const name = $derived(label || L(rule?.label));

	function set(v) {
		value = v;
		onchange?.(v);
	}

	function patch(obj) {
		set({ ...(value || {}), ...obj });
	}

	const RATE_ALL_DEFAULT = { enabled: true, max_actions: 12, max_entities: 4, window_seconds: 60, cooldown_seconds: 300 };

	const RATE_PRESETS = {
		relaxed: { max_actions: 20, window_seconds: 60, cooldown_seconds: 60 },
		medium: { max_actions: 6, window_seconds: 60, cooldown_seconds: 300 },
		strict: { max_actions: 4, window_seconds: 60, cooldown_seconds: 600 },
	};

	let rateMode = $derived.by(() => {
		if (key !== 'kids_rate_limit') return '';
		if (!value) return 'off';
		for (const [id, p] of Object.entries(RATE_PRESETS)) {
			if (p.max_actions === value.max_actions && p.window_seconds === value.window_seconds && p.cooldown_seconds === value.cooldown_seconds) return id;
		}
		return 'custom';
	});

	function intOf(v) {
		const n = Number(v);
		return Number.isFinite(n) ? Math.round(n) : NaN;
	}

	function toggleIn(list, x) {
		const l = list || [];
		return l.includes(x) ? l.filter((y) => y !== x) : [...l, x];
	}

	let types = $derived([...new Set([...FALLBACK_TYPES, ...(Array.isArray(value) && key === 'kids_blocked_types' ? value : [])])]);
	let areas = $derived(ctx.areas || []);
	let wakeEntities = $derived((ctx.entities || []).filter((e) => PANEL_WAKE_DOMAINS.includes(String(e.entity_id).split('.')[0])));
	let luxEntities = $derived(
		(ctx.entities || []).filter((e) => String(e.entity_id).startsWith('sensor.') && (e.device_class === 'illuminance' || e.entity_id === value?.lux_entity)),
	);
	let wakeSearch = $state('');
	let wakeMatches = $derived(
		wakeSearch.trim()
			? wakeEntities
					.filter((e) => !(value?.entities || []).includes(e.entity_id) && `${e.friendly_name} ${e.entity_id}`.toLowerCase().includes(wakeSearch.trim().toLowerCase()))
					.slice(0, 8)
			: [],
	);
	const entityName = (id) => (ctx.entities || []).find((e) => e.entity_id === id)?.friendly_name || id;
	const BRIGHTNESS_DEFAULTS = {
		system: { mode: 'system' },
		fixed: { mode: 'fixed', level: 60 },
		lux: { mode: 'lux', lux_entity: null, min: 10, max: 90, lux_max: 500, curve: 'log' },
		schedule: { mode: 'schedule', schedule: [{ at: '07:00', level: 80 }, { at: '21:00', level: 30 }] },
	};
	let minuteOptions = $derived([...new Set([...PANEL_MINUTES, ...(typeof value === 'number' ? [value] : [])])].sort((a, b) => a - b));
</script>

<div class="rv" data-testid="rule-value-{key}">
	{#if key === 'areas'}
		<RadioSeg
			size="even full"
			label={name}
			value={value?.mode || 'all'}
			options={[
				{ value: 'all', label: t('profiles.v.allAreas') },
				{ value: 'selected', label: t('profiles.v.selection') },
			]}
			onchange={(m) => patch({ mode: m, area_ids: value?.area_ids || [], tags: value?.tags || [], own_area: !!value?.own_area })}
			testid="{idPrefix}-areas-mode"
		/>
		{#if value?.mode === 'selected'}
			<label class="check own">
				<input type="checkbox" checked={!!value.own_area} {disabled} onchange={(e) => patch({ own_area: e.currentTarget.checked })} data-testid="{idPrefix}-own-area" />
				<span><b>{t('profiles.v.ownAreaPlus')}</b> <span class="t-help">{ctx.ownAreaName ? `(${ctx.ownAreaName})` : t('profiles.v.ownAreaHint')}</span></span>
			</label>
			<div class="grid2" role="group" aria-label={t('profiles.v.areasList')}>
				{#each areas as a (a.id)}
					<label class="check">
						<input type="checkbox" checked={(value.area_ids || []).includes(a.id)} {disabled} onchange={() => patch({ area_ids: toggleIn(value.area_ids, a.id) })} />
						<span class="truncate">{a.name}</span>
					</label>
				{/each}
			</div>
			{#if ctx.tags?.length}
				<div class="row" role="group" aria-label={t('profiles.v.tags')}>
					{#each ctx.tags as tag (tag)}
						<button type="button" class="chip" aria-pressed={(value.tags || []).includes(tag) ? 'true' : 'false'} {disabled} onclick={() => patch({ tags: toggleIn(value.tags, tag) })}>#{tag}</button>
					{/each}
				</div>
			{/if}
			{#if !value.own_area && !(value.area_ids || []).length && !(value.tags || []).length}
				<p class="t-help warnline"><Icon name="warn" size="14" />{t('profiles.v.nothingSelected')}</p>
			{/if}
		{/if}
	{:else if key === 'remote_control'}
		<RemoteControlEditor {value} {ctx} {disabled} {idPrefix} onchange={set} />
	{:else if key === 'kids_exceptions'}
		<KidsExceptionsEditor {value} {ctx} {disabled} onchange={set} />
	{:else if key === 'kids_blocked_types'}
		<div class="types" role="group" aria-label={name}>
			{#each types as type (type)}
				<button type="button" class="chip tchip" aria-pressed={(value || []).includes(type) ? 'true' : 'false'} title={typeLabel(type)} {disabled} onclick={() => set(toggleIn(value, type))} data-testid="type-{type}">
					{#if (value || []).includes(type)}<Icon name="lock" size="14" />{/if}
					<span class="truncate">{typeLabel(type)}</span>
				</button>
			{/each}
		</div>
		{#if (value || []).some((x) => safetyTypes().includes(x))}
			<p class="t-help">{t('profiles.safety.kidsVisible')}</p>
		{/if}
	{:else if key === 'access_hours'}
		<Switch
			checked={!!value?.enabled}
			label={value?.enabled ? t('profiles.accessHours.on') : t('profiles.accessHours.always')}
			{disabled}
			labelHidden={false}
			onchange={(v) => (set({ enabled: v, windows: value?.windows?.length ? clone(value.windows) : [{ days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], start: '08:00', end: '20:00' }] }), true)}
			testid="{idPrefix}-access-on"
		/>
		{#if value?.enabled}
			<QuietHoursEditor windows={clone(value.windows || [])} idPrefix="{idPrefix}-ah" noneText={t('profiles.accessHours.never')} onchange={(w) => patch({ windows: clone(w) })} />
		{/if}
		<p class="t-help">{t('profiles.accessHours.hint')}</p>
	{:else if key === 'kids_quiet_hours'}
		<QuietHoursEditor windows={clone(value || [])} idPrefix="{idPrefix}-qh" onchange={(w) => set(clone(w))} />
	{:else if key === 'kids_rate_limit'}
		<RadioSeg
			size="even full"
			label={name}
			value={rateMode}
			options={[
				{ value: 'off', label: t('profiles.rate.off') },
				{ value: 'relaxed', label: t('profiles.rate.relaxed') },
				{ value: 'medium', label: t('profiles.rate.medium') },
				{ value: 'strict', label: t('profiles.rate.strict') },
				{ value: 'custom', label: t('profiles.rate.custom') },
			]}
			onchange={(m) => set(m === 'off' ? null : m === 'custom' ? clone(value || RATE_PRESETS.medium) : clone(RATE_PRESETS[m]))}
		/>
		{#if value && rateMode === 'custom'}
			<div class="nums">
				<label class="nfield"><span class="t-label">{t('kids.rate.max')}</span><input type="number" class="input" min="1" max="100" value={value.max_actions} {disabled} oninput={(e) => patch({ max_actions: intOf(e.currentTarget.value) })} /></label>
				<label class="nfield"><span class="t-label">{t('kids.rate.window')}</span><input type="number" class="input" min="5" max="3600" step="5" value={value.window_seconds} {disabled} oninput={(e) => patch({ window_seconds: intOf(e.currentTarget.value) })} /></label>
				<label class="nfield"><span class="t-label">{t('kids.rate.pause')}</span><input type="number" class="input" min="0" max="1440" value={Math.round(value.cooldown_seconds / 60)} {disabled} oninput={(e) => patch({ cooldown_seconds: intOf(e.currentTarget.value) * 60 })} /></label>
			</div>
			{#if !rateValid(value)}<p class="t-err text-sm">{t('kids.rate.invalid')}</p>{/if}
		{/if}
		<p class="t-help">{summarize('rate_limit', value)}</p>
	{:else if key === 'kids_rate_limit_all'}
		<Switch
			checked={!!value?.enabled}
			label={value?.enabled ? t('profiles.v.on') : t('profiles.v.off')}
			{disabled}
			labelHidden={false}
			onchange={(v) => (set({ ...RATE_ALL_DEFAULT, ...(value || {}), enabled: v }), true)}
			testid="{idPrefix}-rate-all-on"
		/>
		{#if value?.enabled}
			<div class="nums four">
				<label class="nfield"><span class="t-label">{t('kids.rateAll.entities')}</span><input type="number" class="input" min="1" max="50" value={value.max_entities} {disabled} oninput={(e) => patch({ max_entities: intOf(e.currentTarget.value) })} data-testid="{idPrefix}-rate-all-entities" /></label>
				<label class="nfield"><span class="t-label">{t('kids.rateAll.max')}</span><input type="number" class="input" min="1" max="200" value={value.max_actions} {disabled} oninput={(e) => patch({ max_actions: intOf(e.currentTarget.value) })} data-testid="{idPrefix}-rate-all-actions" /></label>
				<label class="nfield"><span class="t-label">{t('kids.rateAll.window')}</span><input type="number" class="input" min="10" max="3600" step="5" value={value.window_seconds} {disabled} oninput={(e) => patch({ window_seconds: intOf(e.currentTarget.value) })} data-testid="{idPrefix}-rate-all-window" /></label>
				<label class="nfield"><span class="t-label">{t('kids.rateAll.pause')}</span><input type="number" class="input" min="1" max="1440" value={Math.round(value.cooldown_seconds / 60)} {disabled} oninput={(e) => patch({ cooldown_seconds: intOf(e.currentTarget.value) * 60 })} data-testid="{idPrefix}-rate-all-pause" /></label>
			</div>
			{#if !rateAllValid(value)}<p class="t-err text-sm">{t('kids.rateAll.invalid')}</p>{/if}
		{/if}
		<p class="t-help" data-testid="{idPrefix}-rate-all-summary">{summarize('rate_limit_all', value)}</p>
		<p class="t-help">{t('kids.rateAll.hint')}</p>
	{:else if key === 'setup_devices'}
		<Switch
			checked={!!value?.enabled}
			label={value?.enabled ? t('profiles.v.on') : t('profiles.v.off')}
			{disabled}
			labelHidden={false}
			onchange={(v) => (patch({ enabled: v, kinds: value?.kinds?.length ? value.kinds : ['guest'] }), true)}
			testid="{idPrefix}-setup-on"
		/>
		{#if value?.enabled}
			<div class="types" role="group" aria-label={t('profiles.kind.which')}>
				{#each profileKinds() as k (k.id)}
					<button
						type="button"
						class="chip tchip"
						aria-pressed={(value.kinds || []).includes(k.id) ? 'true' : 'false'}
						title={kindLabel(k.id, true)}
						{disabled}
						onclick={() => patch({ kinds: profileKinds().map((x) => x.id).filter((x) => (x === k.id ? !(value.kinds || []).includes(x) : (value.kinds || []).includes(x))) })}
						data-testid="{idPrefix}-setup-kind-{k.id}"
					>
						<Icon name={kindIcon(k.id)} size="14" />
						<span class="truncate">{kindLabel(k.id, true)}</span>
					</button>
				{/each}
			</div>
			{#if !(value.kinds || []).length}<p class="t-help warnline"><Icon name="warn" size="14" />{t('profiles.kind.noneHint')}</p>{/if}
		{/if}
		<p class="t-help">{t('profiles.kind.setupHint')}</p>
	{:else if key === 'kids_requests'}
		<Switch checked={!!value?.enabled} label={value?.enabled ? t('profiles.v.on') : t('profiles.v.off')} {disabled} labelHidden={false} onchange={(v) => (patch({ enabled: v, categories: value?.categories?.length ? value.categories : [...REQUEST_KINDS] }), true)} testid="{idPrefix}-requests-on" />
		{#if value?.enabled}
			<div class="grid2" role="group" aria-label={t('family.requests.kinds')}>
				{#each REQUEST_KINDS as c (c)}
					<label class="check">
						<input type="checkbox" checked={(value.categories || []).includes(c)} {disabled} onchange={() => patch({ categories: REQUEST_KINDS.filter((x) => (x === c ? !(value.categories || []).includes(c) : (value.categories || []).includes(x))) })} data-testid="{idPrefix}-requests-{c}" />
						<span class="truncate">{t(`family.category.${c}`)}</span>
					</label>
				{/each}
			</div>
			{#if !(value.categories || []).length}<p class="t-help warnline"><Icon name="warn" size="14" />{t('family.requests.noKinds')}</p>{/if}
		{/if}
		<p class="t-help">{t('family.requests.ruleHint')}</p>
	{:else if key === 'kids_volume_max'}
		<label class="check">
			<input type="checkbox" checked={value != null} {disabled} onchange={(e) => set(e.currentTarget.checked ? 40 : null)} data-testid="{idPrefix}-volume-on" />
			{t('profiles.v.volumeLimit')}
		</label>
		{#if value != null}
			<div class="vol">
				<input type="range" min="0" max="100" step="5" {value} {disabled} oninput={(e) => set(intOf(e.currentTarget.value))} aria-label={name} />
				<span class="vnum">{value} %</span>
			</div>
		{/if}
	{:else if key === 'power_button'}
		<Switch checked={!!value?.enabled} label={value?.enabled ? t('profiles.v.on') : t('profiles.v.off')} {disabled} labelHidden={false} onchange={(v) => (patch({ enabled: v }), true)} />
		{#if value?.enabled}
			<div class="row" role="group" aria-label={t('profiles.v.powerDomains')}>
				{#each POWER_DOMAINS as d (d)}
					<button type="button" class="chip" aria-pressed={(value.domains || []).includes(d) ? 'true' : 'false'} {disabled} onclick={() => patch({ domains: toggleIn(value.domains, d) })}>{t(`profiles.domains.${d}`)}</button>
				{/each}
			</div>
		{/if}
	{:else if key === 'panel_start'}
		<RadioSeg
			size="even full"
			label={name}
			value={value?.mode || 'overview'}
			options={[
				{ value: 'overview', label: t('profiles.v.overview') },
				{ value: 'own_area', label: t('profiles.v.ownArea') },
				{ value: 'area', label: t('profiles.v.area') },
			]}
			onchange={(m) => set(m === 'area' ? { mode: 'area', area_id: value?.area_id || areas[0]?.id || null } : { mode: m, area_id: null })}
		/>
		{#if value?.mode === 'area'}
			<label class="sr-only" for="{idPrefix}-pstart">{t('profiles.v.area')}</label>
			<select id="{idPrefix}-pstart" class="select" value={value.area_id || ''} {disabled} onchange={(e) => patch({ area_id: e.currentTarget.value })}>
				{#each areas as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
			</select>
		{/if}
	{:else if key === 'panel_night'}
		<RadioSeg
			size="even full"
			label={name}
			value={value?.mode || 'off'}
			options={[
				{ value: 'off', label: t('profiles.v.off') },
				{ value: 'schedule', label: t('profiles.v.schedule') },
				{ value: 'sun', label: t('profiles.v.sun') },
			]}
			onchange={(m) => patch({ mode: m })}
		/>
		{#if value && value.mode !== 'off'}
			<div class="nums">
				{#if value.mode === 'schedule'}
					<label class="nfield"><span class="t-label">{t('kids.quiet.from')}</span><input class="input" inputmode="numeric" pattern="[0-2][0-9]:[0-5][0-9]" maxlength="5" value={value.start} {disabled} onchange={(e) => patch({ start: e.currentTarget.value })} /></label>
					<label class="nfield"><span class="t-label">{t('kids.quiet.to')}</span><input class="input" inputmode="numeric" pattern="[0-2][0-9]:[0-5][0-9]" maxlength="5" value={value.end} {disabled} onchange={(e) => patch({ end: e.currentTarget.value })} /></label>
				{/if}
				<label class="nfield">
					<span class="t-label">{t('profiles.v.brightness')}</span>
					<select class="select" value={value.brightness} {disabled} onchange={(e) => patch({ brightness: intOf(e.currentTarget.value) })}>
						{#each NIGHT_BRIGHTNESS as b (b)}<option value={b}>{b} %</option>{/each}
					</select>
				</label>
			</div>
		{/if}
	{:else if key === 'panel_sensors'}
		<div class="types" role="group" aria-label={name}>
			{#each PANEL_SENSORS as sensor (sensor)}
				<button type="button" class="chip tchip" aria-pressed={(value || []).includes(sensor) ? 'true' : 'false'} {disabled} onclick={() => set(PANEL_SENSORS.filter((x) => (x === sensor ? !(value || []).includes(sensor) : (value || []).includes(x))))} data-testid="{idPrefix}-sensor-{sensor}">
					<span class="truncate">{t(`profiles.panel.sensor.${sensor}`)}</span>
				</button>
			{/each}
		</div>
		<p class="t-help">{t('profiles.panel.sensorsHint')}</p>
	{:else if key === 'panel_wake'}
		{#if (value?.entities || []).length}
			<ul class="wake" aria-label={name}>
				{#each value.entities as id (id)}
					<li>
						<span class="truncate" title={id}>{entityName(id)}</span>
						<button type="button" class="btn btn-ghost btn-sm" {disabled} aria-label={t('profiles.panel.removeWake', { name: entityName(id) })} onclick={() => patch({ entities: value.entities.filter((x) => x !== id) })}><Icon name="x" size="14" /></button>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="t-help">{t('profiles.panel.wakeNone')}</p>
		{/if}
		{#if (value?.entities || []).length < 10}
			<label class="sr-only" for="{idPrefix}-wake-search">{t('profiles.panel.wakeAdd')}</label>
			<input id="{idPrefix}-wake-search" class="input" type="search" placeholder={t('profiles.panel.wakeAdd')} bind:value={wakeSearch} {disabled} data-testid="{idPrefix}-wake-search" />
			{#each wakeMatches as e (e.entity_id)}
				<button type="button" class="chip pick" {disabled} onclick={() => (patch({ entities: [...(value?.entities || []), e.entity_id] }), (wakeSearch = ''))}>
					<span class="truncate">{e.friendly_name}</span><span class="t-help truncate">{e.entity_id}</span>
				</button>
			{/each}
		{/if}
		<p class="t-help">{t('profiles.panel.wakeHint')}</p>
		<label class="check" data-testid="{idPrefix}-wake-proximity">
			<input type="checkbox" checked={value?.proximity === true} {disabled} onchange={(e) => patch({ entities: value?.entities || [], proximity: e.currentTarget.checked })} />
			<span>{t('profiles.panel.proximity')}</span>
		</label>
		<p class="t-help">{t('profiles.panel.proximityHint')}</p>
	{:else if key === 'panel_announce'}
		<RadioSeg
			size="even full"
			label={name}
			value={value?.enabled === true}
			options={[
				{ value: false, label: t('profiles.v.off') },
				{ value: true, label: t('profiles.v.on') },
			]}
			onchange={(on) => patch({ enabled: on, volume_max: value?.volume_max ?? 80, respect_night: value?.respect_night ?? true })}
			testid="{idPrefix}-announce"
		/>
		{#if value?.enabled}
			<label class="nfield">
				<span class="t-label">{t('profiles.panel.volumeMax')}</span>
				<select class="select" value={value.volume_max} {disabled} onchange={(e) => patch({ volume_max: intOf(e.currentTarget.value) })} data-testid="{idPrefix}-announce-volume">
					{#each [...new Set([...ANNOUNCE_VOLUMES, value.volume_max])].sort((a, b) => a - b) as v (v)}<option value={v}>{v} %</option>{/each}
				</select>
			</label>
			<label class="check">
				<input type="checkbox" checked={value.respect_night === true} {disabled} onchange={(e) => patch({ respect_night: e.currentTarget.checked })} data-testid="{idPrefix}-announce-night" />
				<span>{t('profiles.panel.respectNight')}</span>
			</label>
		{/if}
		<p class="t-help">{t('profiles.panel.announceHint')}</p>
	{:else if key === 'panel_brightness'}
		<RadioSeg
			size="even full"
			label={name}
			value={value?.mode === 'device_lux' ? 'lux' : value?.mode || 'system'}
			options={PANEL_BRIGHTNESS_MODES.map((m) => ({ value: m, label: t(`profiles.panel.mode.${m}`) }))}
			onchange={(m) => set(clone(BRIGHTNESS_DEFAULTS[m]))}
			testid="{idPrefix}-brightness-mode"
		/>
		{#if value?.mode === 'fixed'}
			<div class="vol">
				<input type="range" min="5" max="100" step="5" value={value.level} {disabled} oninput={(e) => patch({ level: intOf(e.currentTarget.value) })} aria-label={t('profiles.v.brightness')} />
				<span class="vnum">{value.level} %</span>
			</div>
		{:else if value?.mode === 'lux' || value?.mode === 'device_lux'}
			<label class="nfield">
				<span class="t-label">{t('profiles.panel.luxEntity')}</span>
				<select class="select" value={value.lux_entity || ''} {disabled} onchange={(e) => patch({ lux_entity: e.currentTarget.value || null })} data-testid="{idPrefix}-lux-entity">
					<option value="">{t('profiles.panel.luxChoose')}</option>
					{#each luxEntities as e (e.entity_id)}<option value={e.entity_id}>{e.friendly_name}</option>{/each}
				</select>
			</label>
			<div class="nums">
				<label class="nfield"><span class="t-label">{t('profiles.panel.min')}</span>
					<select class="select" value={value.min} {disabled} onchange={(e) => patch({ min: intOf(e.currentTarget.value) })}>{#each PANEL_LEVELS as l (l)}<option value={l}>{l} %</option>{/each}</select></label>
				<label class="nfield"><span class="t-label">{t('profiles.panel.max')}</span>
					<select class="select" value={value.max} {disabled} onchange={(e) => patch({ max: intOf(e.currentTarget.value) })}>{#each PANEL_LEVELS as l (l)}<option value={l}>{l} %</option>{/each}</select></label>
				<label class="nfield"><span class="t-label">{t('profiles.panel.luxMax')}</span>
					<input class="input" type="number" min="10" max="100000" step="10" value={value.lux_max ?? 500} {disabled} oninput={(e) => patch({ lux_max: intOf(e.currentTarget.value) })} /></label>
			</div>
			{#if value.mode === 'lux' && !value.lux_entity}<p class="t-help warnline"><Icon name="warn" size="14" />{t('profiles.panel.luxMissing')}</p>{/if}
			{#if value.min > value.max}<p class="t-err text-sm">{t('profiles.panel.minMax')}</p>{/if}
			<p class="t-help">{t('profiles.panel.luxHint')}</p>
		{:else if value?.mode === 'schedule'}
			<div class="sched" role="group" aria-label={t('profiles.v.schedule')}>
				{#each value.schedule || [] as row, i (i)}
					<div class="srow">
						<label class="nfield"><span class="t-label">{t('profiles.panel.from')}</span><input class="input" inputmode="numeric" pattern="[0-2][0-9]:[0-5][0-9]" maxlength="5" value={row.at} {disabled} onchange={(e) => patch({ schedule: value.schedule.map((r, j) => (j === i ? { ...r, at: e.currentTarget.value } : r)) })} /></label>
						<label class="nfield"><span class="t-label">{t('profiles.v.brightness')}</span>
							<select class="select" value={row.level} {disabled} onchange={(e) => patch({ schedule: value.schedule.map((r, j) => (j === i ? { ...r, level: intOf(e.currentTarget.value) } : r)) })}>{#each PANEL_LEVELS as l (l)}<option value={l}>{l} %</option>{/each}</select></label>
						<button type="button" class="btn btn-ghost btn-sm" disabled={disabled || value.schedule.length <= 1} aria-label={t('profiles.panel.removeRow')} onclick={() => patch({ schedule: value.schedule.filter((_, j) => j !== i) })}><Icon name="x" size="14" /></button>
					</div>
				{/each}
			</div>
			{#if (value.schedule || []).length < 8}
				<button type="button" class="btn btn-secondary btn-sm" {disabled} onclick={() => patch({ schedule: [...(value.schedule || []), { at: '12:00', level: 60 }] })}>{t('profiles.panel.addRow')}</button>
			{/if}
		{/if}
		<p class="t-help">{t('profiles.panel.brightnessHint')}</p>
	{:else if key === 'home_header'}
		{@const hv = { ...HOME_HEADER_DEFAULT, ...(value || {}) }}
		<div class="hstyles" role="radiogroup" aria-label={t('profiles.home.style')}>
			{#each HOME_STYLES as s (s)}
				<button
					type="button"
					role="radio"
					class="hcard"
					aria-checked={hv.style === s ? 'true' : 'false'}
					{disabled}
					onclick={() => set({ ...hv, style: s })}
					data-testid="{idPrefix}-style-{s}"
				>
					<span class="hshot">
						<img class="hlight" src="{base}/home-header/{s}-light.jpg" alt={t('profiles.home.preview', { name: enumLabel('home_header', s) })} width="300" height="436" loading="lazy" />
						<img class="hdark" src="{base}/home-header/{s}-dark.jpg" alt="" width="300" height="436" loading="lazy" />
					</span>
					<span class="hname">
						<span class="htitle">{enumLabel('home_header', s)}</span>
						{#if hv.style === s}<Icon name="check" size="16" />{/if}
					</span>
					<span class="t-help hdesc">{t(`profiles.home.${s}Hint`)}</span>
				</button>
			{/each}
		</div>
		<div class="hparts" role="group" aria-label={t('profiles.home.parts')}>
			{#each HOME_PARTS as part (part)}
				<Switch
					checked={hv[part] === true}
					label={t(`profiles.home.${part}`)}
					description={t(`profiles.home.${part}Hint`)}
					{disabled}
					onchange={(on) => (set({ ...hv, [part]: on }), true)}
					testid="{idPrefix}-{part}"
				/>
			{/each}
		</div>
		<p class="t-help">{t('profiles.home.ignored')}</p>
	{:else if key === 'columns'}
		<RadioSeg size="even full" label={name} value={value} options={[1, 2, 3, 4].map((n) => ({ value: n, label: String(n) }))} onchange={set} />
	{:else if key === 'panel_auto_home' || key === 'panel_dim' || key === 'panel_screensaver'}
		<label class="sr-only" for="{idPrefix}-{key}">{name}</label>
		<select id="{idPrefix}-{key}" class="select" {value} {disabled} onchange={(e) => set(intOf(e.currentTarget.value))}>
			{#each minuteOptions as m (m)}<option value={m}>{formatValue(key, m)}</option>{/each}
		</select>
	{:else if rule?.type === 'boolean'}
		<RadioSeg
			size="even full"
			label={name}
			{value}
			options={[
				{ value: false, label: t('profiles.v.off') },
				{ value: true, label: t('profiles.v.on') },
			]}
			onchange={set}
			testid="{idPrefix}-{key}"
		/>
	{:else if rule?.type === 'enum'}
		{@const choices = enumChoices(rule).filter((c) => c === value || !(ctx.hiddenChoices?.[key] || []).includes(c))}
		<RadioSeg size="even full" label={name} {value} options={choices.map((c) => ({ value: c, label: enumLabel(key, c) }))} onchange={set} testid="{idPrefix}-{key}" />
		{#if key === 'panel_off_mode'}
			<p class="t-help">{t('profiles.panel.offModeHint')}</p>
		{/if}
		{#if key === 'notification_level'}
			<p class="t-help safety" data-testid="{idPrefix}-safety-notifications"><Icon name="shieldcheck" size="14" /><span>{t('profiles.safety.note')} {t('profiles.safety.noAlarmSystem')}</span></p>
		{/if}
	{:else if rule?.type === 'integer'}
		<label class="sr-only" for="{idPrefix}-{key}">{name}</label>
		<input id="{idPrefix}-{key}" class="input w-32" type="number" {value} {disabled} oninput={(e) => set(intOf(e.currentTarget.value))} />
	{:else}
		<p class="t-help">{formatValue(key, value, ctx)}</p>
	{/if}
</div>

<style>
	.rv {
		display: grid;
		gap: 8px;
		min-width: 0;
		justify-items: stretch;
	}
	.rv :global(.seg.even) {
		width: 100%;
		box-sizing: border-box;
	}
	.rv :global(.seg.even button) {
		min-width: 0;
		padding: 0 6px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.grid2 {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 2px 12px;
	}
	.grid2 .check {
		min-width: 0;
	}
	.own {
		align-items: flex-start;
	}
	.safety {
		display: flex;
		gap: 6px;
		align-items: flex-start;
		margin: 0;
	}
	.safety :global(.ic) {
		flex: none;
		margin-top: 2px;
	}
	.warnline {
		display: flex;
		gap: 6px;
		align-items: center;
		color: var(--warn-ink);
	}
	/* Equal sizes: equal type buttons in a grid. */
	.types {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 150px), 1fr));
		gap: 8px;
	}
	.tchip {
		justify-content: flex-start;
		min-width: 0;
		min-height: var(--h-ctl);
		overflow: hidden;
	}
	.wake {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 4px;
	}
	.wake li {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		gap: 8px;
	}
	.pick {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 8px;
		text-align: left;
		min-width: 0;
	}
	.sched {
		display: grid;
		gap: 6px;
	}
	.srow {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
		gap: 10px;
		align-items: end;
	}
	.nums {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 10px;
	}
	.nums.four {
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}
	.nfield {
		display: grid;
		gap: 4px;
		min-width: 0;
		align-content: end;
	}
	.nfield .input,
	.nfield .select {
		width: 100%;
		min-width: 0;
	}
	.vol {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 4.5em;
		gap: 10px;
		align-items: center;
	}
	.vol input {
		width: 100%;
		accent-color: var(--accent);
	}
	.vnum {
		font-weight: 650;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	@media (max-width: 639px) {
		.nums,
		.nums.four {
			grid-template-columns: minmax(0, 1fr);
		}
		.grid2 {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	/* Home screen head: two equal style cards (same width, image height and three reserved text lines). */
	.hstyles {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 12px;
	}
	.hcard {
		display: grid;
		grid-template-rows: auto auto auto;
		gap: 6px;
		min-width: 0;
		padding: 8px;
		text-align: left;
		border: 1px solid var(--line-2);
		border-radius: var(--r-card);
		background: var(--surface, transparent);
		color: inherit;
		cursor: pointer;
	}
	.hcard[aria-checked='true'] {
		border-color: var(--accent);
		background: var(--accent-soft);
		box-shadow: 0 0 0 1px var(--accent);
	}
	.hcard:focus-visible {
		outline: 2px solid var(--focus);
		outline-offset: 2px;
	}
	.hcard:disabled {
		cursor: default;
		opacity: 0.6;
	}
	.hshot {
		display: block;
		justify-self: center;
		height: 220px;
		max-width: 100%;
		aspect-ratio: 300 / 436;
		overflow: hidden;
		border-radius: var(--r-box);
		background: var(--line);
	}
	.hshot img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: top;
	}
	.hdark,
	:global(:root[data-theme='dark']) .hlight {
		display: none;
	}
	:global(:root[data-theme='dark']) .hdark {
		display: block;
	}
	.hname {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 6px;
		min-width: 0;
		font-weight: 650;
	}
	.htitle {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	/* Narrow cards: the style name may take two lines, reserved in both cards. */
	@media (max-width: 639px) {
		.htitle {
			display: -webkit-box;
			-webkit-line-clamp: 2;
			line-clamp: 2;
			-webkit-box-orient: vertical;
			white-space: normal;
			overflow-wrap: anywhere;
			hyphens: auto;
			min-height: calc(2 * 1.3em);
			line-height: 1.3;
		}
	}
	.hname :global(.ic) {
		flex: none;
		color: var(--accent-ink);
	}
	.hdesc {
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		min-height: calc(3 * 1.4em);
		line-height: 1.4;
		margin: 0;
	}
	.hparts {
		display: grid;
		gap: 8px;
	}
</style>
