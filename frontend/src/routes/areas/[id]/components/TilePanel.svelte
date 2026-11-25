<script>
	/**
	 * Side panel "Customize tile" of the area editor (mockup
	 * website/tile-settings-mock). Non-modal region next to the editor on wide
	 * screens (role=dialog, aria-modal=false), full-screen sheet with a focus
	 * trap below 1024 px. Live preview iPhone / Apple TV in both card styles;
	 * only options the contract allows for the domain are offered
	 * (GET /api/tiles/capabilities). Saving sends the changed fields only
	 * (PUT /api/areas/{id}/entities/{entity_id}).
	 *
	 * Props: area, entity (editor item incl. state/tile), sectionColumns,
	 * previewEntities, caps ({capabilities, icons}), paths (Map mdi -> path),
	 * dirty (bindable), onclose(), onsaved(), onask(confirmOptions)
	 */
	import { onMount, tick, untrack } from 'svelte';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { i18n, t } from '$lib/i18n/index.svelte.js';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import RadioSeg from '$lib/ui/RadioSeg.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import TilePreview from './TilePreview.svelte';
	import TileIconPicker from './TileIconPicker.svelte';
	import QuickAccessSection from './QuickAccessSection.svelte';
	import {
		ACCENTS,
		FORM_FACTORS,
		HOLD_ACTIONS,
		VARIANT_SIZE,
		capRow,
		cardModel,
		domainOf,
		domainIconPath,
		draftFrom,
		isDirty,
		payload,
		sizeMismatch,
		tapActions,
		tapText,
		validIcon,
	} from '$lib/data/tiles.js';

	let {
		area,
		entity,
		sectionColumns = 2,
		previewEntities = [],
		caps,
		paths,
		dirty = $bindable(false),
		onclose,
		onsaved,
		onask,
	} = $props();

	let draft = $state(null);
	let loadedFor = null;
	let saving = $state(false);
	let errors = $state({});
	let pickerOpen = $state(false);
	let dev = $state('phone');
	let cardStyle = $state('calm');
	let previewOn = $state(true);
	let suggestions = $state(null);
	let suggestionsError = $state('');
	let narrow = $state(false);
	let panel = $state(null);
	let heading = $state(null);
	/** Paths of icons picked from the full MDI catalog (not in the curated set). */
	let extraPaths = $state(new Map());

	let row = $derived(capRow(caps?.capabilities, entity.entity_id));
	let domain = $derived(domainOf(entity.entity_id));
	let haName = $derived(entity.state?.attributes?.friendly_name || entity.entity_id);
	let name = $derived(draft?.display_name?.trim() || entity.display_name || haName);
	let domainLabel = $derived(t(`tiles.domainNames.${domain}`) === `tiles.domainNames.${domain}` ? domain : t(`tiles.domainNames.${domain}`));
	let allPaths = $derived(new Map([...(paths || []), ...extraPaths]));

	// Reset the draft when another tile opens, or when the saved data changes
	// while nothing is edited (live update, own save).
	$effect(() => {
		const e = entity;
		if (loadedFor !== e.entity_id || !untrack(() => isDirty(e, draft))) {
			const switched = loadedFor !== e.entity_id;
			loadedFor = e.entity_id;
			draft = draftFrom(e);
			if (switched) untrack(() => {
				errors = {};
				pickerOpen = false;
				suggestions = null;
				loadSuggestions();
				tick().then(() => heading?.focus({ preventScroll: false }));
			});
		}
	});
	$effect(() => {
		dirty = isDirty(entity, draft);
	});

	onMount(() => {
		const mq = window.matchMedia('(max-width: 1023px)');
		const on = () => (narrow = mq.matches);
		on();
		mq.addEventListener('change', on);
		return () => mq.removeEventListener('change', on);
	});

	async function loadSuggestions() {
		const id = entity.entity_id;
		suggestionsError = '';
		try {
			const res = await api.getTileSuggestions(area.id, id);
			if (entity.entity_id === id) suggestions = res;
		} catch (e) {
			if (entity.entity_id === id) suggestionsError = e.message;
		}
	}

	// ── draft helpers ─────────────────────────────────────────────────

	function setTile(key, value) {
		const tile = { ...draft.tile };
		if (value === null || value === undefined) delete tile[key];
		else tile[key] = value;
		draft = { ...draft, tile };
		errors = Object.fromEntries(Object.entries(errors).filter(([k]) => k !== key && !k.startsWith(`${key}.`)));
	}

	let variant = $derived(draft?.tile.variant || 'compact');
	let tap = $derived(draft?.tile.tap_action?.action || 'default');
	let hold = $derived(draft?.tile.hold_action?.action || 'default');
	let secondary = $derived(draft?.tile.secondary || { source: 'state' });
	let hidden = $derived(draft?.tile.hidden_on || []);
	let mismatch = $derived(draft ? sizeMismatch(draft.tile, row, draft.columns, draft.rows, sectionColumns) : false);
	let needSize = $derived(VARIANT_SIZE[variant] || [1, 1]);

	function setVariant(v) {
		setTile('variant', v === 'compact' ? null : v);
	}

	function fixSize() {
		const [c, r] = needSize;
		draft = { ...draft, columns: Math.max(draft.columns, c), rows: Math.max(draft.rows, r) };
	}

	function setTap(action) {
		if (action === 'default') setTile('tap_action', null);
		else if (action === 'run') {
			const own = row?.run_self ? entity.entity_id : null;
			const first = own || suggestions?.run_targets?.[0]?.entity_id || null;
			setTile('tap_action', first ? { action: 'run', target_entity_id: first } : { action: 'run' });
		} else setTile('tap_action', { action });
		if (!confirmAvailable(action)) setTile('confirm', null);
	}

	function confirmAvailable(action = tap) {
		if (!row?.confirm) return false;
		return action === 'toggle' || action === 'run' || (action === 'default' && row.run_self);
	}

	function setSecondary(source) {
		if (source === 'state') setTile('secondary', null);
		else if (source === 'attribute') setTile('secondary', { source, attribute: row.attributes[0] });
		else if (source === 'entity') {
			const first = suggestions?.sensors?.[0]?.entity_id;
			setTile('secondary', first ? { source, entity_id: first } : { source });
		} else setTile('secondary', { source });
	}

	function toggleHidden(ff, show) {
		const set = new Set(hidden);
		if (show) set.delete(ff);
		else set.add(ff);
		const list = FORM_FACTORS.filter((f) => set.has(f));
		setTile('hidden_on', list.length ? list : null);
	}

	function setIcon(value, path) {
		if (path && value?.startsWith('mdi:') && !paths?.has(value.slice(4))) {
			extraPaths = new Map([...extraPaths, [value.slice(4), path]]);
		}
		setTile('icon', value);
	}

	// ── run target visibility hint (the admin decides) ────────────

	let target = $derived(
		tap === 'run' ? suggestions?.run_targets?.find((x) => x.entity_id === draft.tile.tap_action?.target_entity_id) || null : null,
	);
	let targetMissing = $derived(tap === 'run' && suggestions && draft.tile.tap_action?.target_entity_id && !target);
	let whoList = $derived(
		(target?.not_visible_for || []).map((d) => (d.is_guest ? `${d.name} ${t('tiles.guestSuffix')}` : d.name)).join(', '),
	);

	// ── preview ───────────────────────────────────────────────────────

	function model(e, opts = {}) {
		const isEdit = e.entity_id === entity.entity_id;
		const d = isEdit ? draft : null;
		const r = isEdit ? row : capRow(caps?.capabilities, e.entity_id);
		return cardModel({
			entity: e,
			tile: d ? d.tile : e.tile || {},
			name: (d ? d.display_name?.trim() : e.display_name) || e.state?.attributes?.friendly_name || e.entity_id,
			cols: d ? d.columns : e.columns || 1,
			rows: d ? d.rows : e.rows || 1,
			on: isEdit ? previewOn : null,
			dev: opts.dev,
			row: r,
			paths: allPaths,
			sensors: suggestions?.sensors,
			sectionColumns: opts.sectionColumns ?? 2,
			t,
			locale: i18n.locale,
		});
	}

	let phoneCards = $derived(draft ? previewEntities.filter((e) => e.section_id === entity.section_id).map((e) => model(e, { dev: 'phone', sectionColumns })) : []);
	let tvCards = $derived(draft ? previewEntities.map((e) => model(e, { dev: 'tv' })) : []);
	let editCard = $derived(draft ? model(entity, { dev, sectionColumns }) : null);
	let tapLabel = $derived(draft ? tapText(draft.tile, row, domain, dev, t, target?.name) : '');
	let tvHints = $derived([
		{ key: t('tiles.tvHintSelect'), text: tapLabel },
		{ key: t('tiles.tvHintPlay'), text: t('tiles.tvHintToggle') },
		...(hold === 'none' ? [] : [{ key: t('tiles.tvHintHold'), text: t('tiles.status.tap.details') }]),
		{ key: t('tiles.tvHintMenu'), text: t('tiles.tvHintBack') },
	]);
	let summary = $derived(
		editCard
			? t('tiles.previewSummary', {
					device: dev === 'tv' ? t('tiles.previewTv') : t('tiles.previewPhone'),
					style: cardStyle === 'calm' ? t('tiles.styleCalm') : t('tiles.styleColor'),
					name,
					variant: editCard.hidden ? t('tiles.hiddenHere') : t(`tiles.variants.${editCard.variant}`),
					accent: draft.tile.accent ? `, ${t(`tiles.accents.${draft.tile.accent}`)}` : '',
					tap: tapLabel,
				})
			: '',
	);
	let request = $derived(draft ? JSON.stringify(payload(entity, draft), null, 2) : '');
	let iconInfo = $derived.by(() => {
		const icon = draft?.tile.icon;
		if (!icon) return { label: t('tiles.iconDefault'), sub: t('tiles.iconDefaultHint') };
		const cur = caps?.icons?.icons?.find((i) => `mdi:${i.mdi}` === icon);
		const label = cur ? cur.keywords?.[i18n.locale]?.[0] || cur.mdi : icon;
		return { label, sub: icon, sf: cur?.sf };
	});

	// ── save / reset / close ──────────────────────────────────────────

	function mapErrors(detail) {
		const out = {};
		for (const d of detail || []) {
			const loc = (d.loc || []).filter((x) => x !== 'body');
			// tile.tap_action.target_entity_id -> "tap_action.target_entity_id"
			const key = loc[0] === 'tile' ? loc.slice(1).join('.') || 'tile' : loc[0] || 'tile';
			out[key] = out[key] ? `${out[key]} ${d.msg}` : d.msg;
		}
		return out;
	}

	async function save() {
		if (!dirty || saving) return;
		if (!validIcon(draft.tile.icon)) {
			errors = { icon: t('tiles.iconSfInvalid') };
			return;
		}
		saving = true;
		try {
			await api.updateEntity(area.id, entity.entity_id, payload(entity, draft));
			errors = {};
			addToast(t('tiles.saveDone', { name }), 'success');
			await onsaved?.();
		} catch (e) {
			if (e.status === 422 && e.detail) {
				errors = mapErrors(e.detail);
				addToast(t('tiles.fieldError'), 'error');
				await tick();
				panel?.querySelector('[aria-invalid="true"]')?.focus();
			} else addToast(e.status === 503 ? t('tiles.haUnavailable') : t('tiles.saveFailed', { message: e.message }), 'error');
		} finally {
			saving = false;
		}
	}

	function reset() {
		onask?.({
			title: t('tiles.resetTitle'),
			message: t('tiles.resetConfirm', { name }),
			confirmLabel: t('tiles.reset'),
			action: async () => {
				try {
					await api.updateEntity(area.id, entity.entity_id, { tile: null });
					draft = { ...draft, tile: {} };
					errors = {};
					addToast(t('tiles.resetDone', { name }), 'success');
					await onsaved?.();
				} catch (e) {
					addToast(t('tiles.saveFailed', { message: e.message }), 'error');
				}
			},
		});
	}

	function cancel() {
		draft = draftFrom(entity);
		errors = {};
		onclose?.(true);
	}

	function onkeydown(e) {
		if (e.key === 'Escape' && !e.defaultPrevented) {
			e.preventDefault();
			onclose?.();
			return;
		}
		if (e.key === 'Tab' && narrow && panel) {
			const items = [...panel.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea, [tabindex="0"]')].filter((el) => el.offsetParent !== null);
			if (!items.length) return;
			const first = items[0];
			const last = items[items.length - 1];
			if (e.shiftKey && (document.activeElement === first || document.activeElement === heading)) {
				e.preventDefault();
				last.focus();
			} else if (!e.shiftKey && document.activeElement === last) {
				e.preventDefault();
				first.focus();
			}
		}
	}

	const fieldId = (k) => `tile-f-${k}`;
	/** Error text for a field: exact key or any nested key below it. */
	function fieldError(prefix) {
		const msgs = Object.entries(errors)
			.filter(([k, v]) => v && (k === prefix || k.startsWith(`${prefix}.`)))
			.map(([, v]) => v);
		return msgs.join(' ');
	}
</script>

{#if draft}
	<div
		id="tile-panel"
		class="tpanel"
		class:sheet={narrow}
		role="dialog"
		tabindex="-1"
		aria-modal={narrow ? 'true' : 'false'}
		aria-labelledby="tile-panel-title"
		bind:this={panel}
		{onkeydown}
		data-testid="tile-panel"
	>
		<header class="phead">
			<span class="chipic"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d={editCard?.path || domainIconPath(entity.entity_id)} /></svg></span>
			<div class="min-w-0 flex-1">
				<h2 id="tile-panel-title" tabindex="-1" bind:this={heading}>{t('tiles.panelTitle', { name })}</h2>
				<small class="t-mono">{entity.entity_id}</small>
				{#if dirty}<span class="dirty"> · {t('tiles.unsaved')}</span>{/if}
			</div>
			<button type="button" class="btn btn-icon btn-sm" onclick={() => onclose?.()} aria-label={t('tiles.close')} data-testid="tile-close"><Icon name="x" /></button>
		</header>

		<div class="pbody">
			<div class="pv">
				<div class="pvtabs">
					<RadioSeg size="seg-sm" label={t('tiles.previewDevice')} options={[{ value: 'phone', label: t('tiles.previewPhone') }, { value: 'tv', label: t('tiles.previewTv') }]} value={dev} onchange={(v) => (dev = v)} testid="tile-pv-device" />
					<RadioSeg size="seg-sm" label={t('tiles.previewStyle')} options={[{ value: 'calm', label: t('tiles.styleCalm') }, { value: 'color', label: t('tiles.styleColor') }]} value={cardStyle} onchange={(v) => (cardStyle = v)} testid="tile-pv-style" />
					{#if !row?.run_self}
						<RadioSeg size="seg-sm" label={t('tiles.previewState')} options={[{ value: true, label: t('tiles.stateOn') }, { value: false, label: t('tiles.stateOff') }]} value={previewOn} onchange={(v) => (previewOn = v)} testid="tile-pv-state" />
					{/if}
				</div>
				<TilePreview
					dev={dev}
					style={cardStyle}
					cards={dev === 'tv' ? tvCards : phoneCards}
					editId={entity.entity_id}
					title={area.name}
					{tvHints}
				/>
				<p class="pvsum" aria-live="polite" data-testid="tile-pv-summary">{summary}</p>
			</div>

			<Banner tone="info" live={false} icon="sparkle" class="pro">{t('tiles.proNote')}</Banner>

			<div class="grp">
				<h3>{t('tiles.sectionLook')}</h3>
				<div class="field">
					<label class="field-label" for={fieldId('name')}>{t('tiles.displayName')}</label>
					<input
						id={fieldId('name')}
						class="input"
						maxlength="100"
						placeholder={haName}
						bind:value={draft.display_name}
						aria-invalid={fieldError('display_name') ? 'true' : undefined}
						aria-describedby="{fieldId('name')}-hint{fieldError('display_name') ? ' tile-err-display_name' : ''}"
						data-testid="tile-name"
					/>
					{#if fieldError('display_name')}<p id="tile-err-display_name" class="field-error">{fieldError('display_name')}</p>{/if}
					<p id="{fieldId('name')}-hint" class="t-help">{t('tiles.displayNameHint', { name: haName })}</p>
				</div>

				<div class="field">
					<span class="field-label" id={fieldId('icon')}>{t('tiles.icon')}</span>
					{#if row?.icon}
						<button
							type="button"
							class="iconbtn"
							aria-expanded={pickerOpen ? 'true' : 'false'}
							aria-controls="tile-icon-picker"
							aria-labelledby="{fieldId('icon')} tile-icon-current"
							class:invalid={!!fieldError('icon')}
							onclick={() => (pickerOpen = !pickerOpen)}
							data-testid="tile-icon-button"
						>
							<span class="chipic"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d={editCard?.path || domainIconPath(entity.entity_id)} /></svg></span>
							<span class="flex-1 min-w-0 iconbtn-text">
								<span id="tile-icon-current">{iconInfo.label}</span>
								<small class:t-mono={!!draft.tile.icon}>{iconInfo.sub}</small>
							</span>
							<Icon name={pickerOpen ? 'chevdown' : 'chev'} />
						</button>
						{#if pickerOpen}
							<div id="tile-icon-picker">
								<TileIconPicker
									value={draft.tile.icon || null}
									icons={caps?.icons?.icons || []}
									categories={caps?.icons?.categories || []}
									locale={i18n.locale}
									onchange={setIcon}
								/>
							</div>
						{/if}
						{#if fieldError('icon')}<p class="field-error" role="alert">{fieldError('icon')}</p>{/if}
						<p class="t-help">{t('tiles.iconStatic')}{#if iconInfo.sf}{' '}{t('tiles.iconSfPair', { sf: iconInfo.sf })}{/if}</p>
					{:else}
						<p class="locked">{t('tiles.iconLocked', { domain: domainLabel })}</p>
					{/if}
				</div>

				<div class="field">
					<span class="field-label" id={fieldId('variant')}>{t('tiles.variant')}</span>
					<RadioSeg
						labelledby={fieldId('variant')}
						options={(row?.variants || ['compact']).map((v) => ({ value: v, label: t(`tiles.variants.${v}`) }))}
						value={variant}
						onchange={setVariant}
						invalid={fieldError('variant') ? 'true' : undefined}
						testid="tile-variant"
					/>
					{#if fieldError('variant')}<p class="field-error" role="alert">{fieldError('variant')}</p>{/if}
					{#if mismatch}
						<div class="scope warn" data-testid="tile-size-warn">
							{#if sectionColumns < 2 && variant === 'slider'}
								{t('tiles.variantSingleColumn')}
							{:else}
								{t('tiles.variantSize', { size: `${needSize[0]}×${needSize[1]}` })}
								<button type="button" class="linkish" onclick={fixSize}>{t('tiles.variantSizeFix', { size: `${needSize[0]}×${needSize[1]}` })}</button>
							{/if}
						</div>
					{/if}
					{#if variant === 'slider'}<p class="t-help">{t('tiles.sliderTv')}</p>{/if}
				</div>

				<div class="field">
					<span class="field-label" id={fieldId('accent')}>{t('tiles.accent')}</span>
					{#if row?.accent}
						<div class="swatches" role="radiogroup" aria-labelledby={fieldId('accent')} aria-invalid={fieldError('accent') ? 'true' : undefined} data-testid="tile-accent">
							{#each [null, ...ACCENTS] as a, i (a ?? 'auto')}
								{@const checked = (draft.tile.accent || null) === a}
								<button
									type="button"
									role="radio"
									class="swatch"
									aria-checked={checked ? 'true' : 'false'}
									tabindex={checked ? 0 : -1}
									title={a ? t(`tiles.accents.${a}`) : t('tiles.accentAuto')}
									onclick={() => setTile('accent', a)}
									onkeydown={(e) => {
										const list = [null, ...ACCENTS];
										const map = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
										if (!(e.key in map)) return;
										e.preventDefault();
										const next = (i + map[e.key] + list.length) % list.length;
										setTile('accent', list[next]);
										e.currentTarget.parentElement.children[next]?.focus();
									}}
									data-accent={a ?? 'auto'}
								>
									<i class="sw sw-{a ?? 'auto'}">{#if checked}<Icon name="check" />{/if}</i>
									<span>{a ? t(`tiles.accents.${a}`) : t('tiles.accentAuto')}</span>
								</button>
							{/each}
						</div>
						<p class="t-help">{t('tiles.accentHint')}</p>
					{:else}
						<p class="locked">{t('tiles.accentLocked', { domain: domainLabel })}</p>
					{/if}
				</div>
			</div>

			<div class="grp">
				<h3>{t('tiles.sectionContent')}</h3>
				<div class="field">
					<label class="field-label" for={fieldId('secondary')}>{t('tiles.secondary')}</label>
					<select
						id={fieldId('secondary')}
						class="input"
						value={secondary.source}
						onchange={(e) => setSecondary(e.currentTarget.value)}
						aria-invalid={fieldError('secondary') ? 'true' : undefined}
						data-testid="tile-secondary"
					>
						<option value="state">{t('tiles.secondaryOpts.state')}</option>
						<option value="last_changed">{t('tiles.secondaryOpts.last_changed')}</option>
						{#if row?.attributes?.length}<option value="attribute">{t('tiles.secondaryOpts.attribute')}</option>{/if}
						{#if row?.secondary_entity}<option value="entity">{t('tiles.secondaryOpts.entity')}</option>{/if}
						<option value="none">{t('tiles.secondaryOpts.none')}</option>
					</select>
					{#if secondary.source === 'attribute'}
						<label class="field-label mt-2" for={fieldId('attribute')}>{t('tiles.secondaryAttribute')}</label>
						<select id={fieldId('attribute')} class="input" value={secondary.attribute} onchange={(e) => setTile('secondary', { source: 'attribute', attribute: e.currentTarget.value })}>
							{#each row.attributes as a (a)}<option value={a}>{t(`tiles.attributes.${a}`)}</option>{/each}
						</select>
					{:else if secondary.source === 'entity'}
						<label class="field-label mt-2" for={fieldId('sensor')}>{t('tiles.secondaryEntity')}</label>
						{#if !suggestions && !suggestionsError}
							<p class="t-help">{t('tiles.secondaryLoading')}</p>
						{:else}
							<select id={fieldId('sensor')} class="input" value={secondary.entity_id || ''} onchange={(e) => setTile('secondary', { source: 'entity', entity_id: e.currentTarget.value })} data-testid="tile-sensor">
								{#if secondary.entity_id && !suggestions?.sensors?.some((s) => s.entity_id === secondary.entity_id)}
									<option value={secondary.entity_id}>{secondary.entity_id} · {t('tiles.secondaryEntityMissing')}</option>
								{/if}
								{#if suggestions?.sensors?.some((s) => s.same_device)}
									<optgroup label={t('tiles.secondaryEntitySame')}>
										{#each suggestions.sensors.filter((s) => s.same_device) as s (s.entity_id)}<option value={s.entity_id}>{s.name} ({s.state}{s.unit ? ` ${s.unit}` : ''})</option>{/each}
									</optgroup>
								{/if}
								<optgroup label={t('tiles.secondaryEntityOther')}>
									{#each (suggestions?.sensors || []).filter((s) => !s.same_device) as s (s.entity_id)}<option value={s.entity_id}>{s.name} ({s.state}{s.unit ? ` ${s.unit}` : ''})</option>{/each}
								</optgroup>
							</select>
						{/if}
					{/if}
					{#if fieldError('secondary')}<p class="field-error" role="alert">{fieldError('secondary')}</p>{/if}
				</div>
			</div>

			<div class="grp">
				<h3>{t('tiles.sectionControl')}</h3>
				<div class="field">
					<label class="field-label" for={fieldId('tap')}>{t('tiles.tap')}</label>
					<select id={fieldId('tap')} class="input" value={tap} onchange={(e) => setTap(e.currentTarget.value)} aria-invalid={errors['tap_action'] || errors['tap_action.action'] ? 'true' : undefined} data-testid="tile-tap">
						{#each tapActions(caps?.capabilities, row) as a (a)}
							<option value={a}>{a === 'default' ? t('tiles.tapDefaultIs', { what: tapText({}, row, domain, 'phone', t) }) : t(`tiles.actions.${a}`)}</option>
						{/each}
					</select>
					{#if !row?.tap_actions?.length}<p class="t-help">{t('tiles.tapOnlyBasic', { domain: domainLabel })}</p>{/if}
				</div>
				{#if tap === 'run'}
					<div class="field">
						<label class="field-label" for={fieldId('target')}>{t('tiles.runTarget')}</label>
						<select
							id={fieldId('target')}
							class="input"
							value={draft.tile.tap_action?.target_entity_id || ''}
							onchange={(e) => setTile('tap_action', { action: 'run', target_entity_id: e.currentTarget.value })}
							aria-invalid={errors['tap_action.target_entity_id'] ? 'true' : undefined}
							data-testid="tile-target"
						>
							{#if targetMissing}<option value={draft.tile.tap_action.target_entity_id}>{draft.tile.tap_action.target_entity_id} · {t('tiles.runTargetMissing')}</option>{/if}
							{#if !draft.tile.tap_action?.target_entity_id}<option value="" disabled>—</option>{/if}
							{#each suggestions?.run_targets || [] as x (x.entity_id)}<option value={x.entity_id}>{x.name} · {x.entity_id}</option>{/each}
						</select>
						{#if targetMissing}
							<p class="scope err">{t('tiles.runTargetMissing')}</p>
						{:else if target}
							{#if target.not_visible_for.length}
								<p class="scope warn" data-testid="tile-scope-warn">
									<Icon name="warn" />
									{#if !target.areas.length}{t('tiles.runNotPlaced', { target: target.name })}{' '}{/if}
									{t('tiles.runScopeWarn', { target: target.name, who: whoList })}
								</p>
							{:else if suggestions.tile_viewers.length === 0}
								<p class="scope">{t('tiles.runScopeNobody')}</p>
							{:else}
								<p class="scope ok"><Icon name="check" />{t('tiles.runVisibleAll', { areas: target.areas.map((a) => a.name).join(', ') })}</p>
							{/if}
						{/if}
						{#if fieldError('tap_action')}<p class="field-error" role="alert">{fieldError('tap_action')}</p>{/if}
						<p class="t-help">{t('tiles.runTargetHint')}</p>
					</div>
				{/if}
				{#if confirmAvailable()}
					<Switch checked={!!draft.tile.confirm} label={t('tiles.confirm')} onchange={(v) => setTile('confirm', v ? true : null)} testid="tile-confirm" />
				{:else if row?.confirm_always}
					<p class="t-help">{t('tiles.confirmAlways', { domain: domainLabel })}</p>
				{/if}
				<div class="field mt-2">
					<label class="field-label" for={fieldId('hold')}>{t('tiles.hold')}</label>
					<select id={fieldId('hold')} class="input" value={hold} onchange={(e) => setTile('hold_action', e.currentTarget.value === 'default' ? null : { action: e.currentTarget.value })} data-testid="tile-hold">
						{#each HOLD_ACTIONS as a (a)}<option value={a}>{a === 'default' ? t('tiles.holdDefault') : t(`tiles.actions.${a}`)}</option>{/each}
					</select>
				</div>
			</div>

			<div class="grp">
				<h3 id={fieldId('showon')}>{t('tiles.showOn')}</h3>
				<div class="devs" role="group" aria-labelledby={fieldId('showon')} data-testid="tile-show-on">
					{#each FORM_FACTORS as ff (ff)}
						{@const shown = !hidden.includes(ff)}
						<label class="chk">
							<input type="checkbox" checked={shown} disabled={shown && hidden.length >= 2} onchange={(e) => toggleHidden(ff, e.currentTarget.checked)} data-ff={ff} />
							<Icon name={ff === 'phone' ? 'iphone' : ff === 'tablet' ? 'ipad' : 'tv'} />{t(`tiles.formFactors.${ff}`)}
						</label>
					{/each}
				</div>
				{#if hidden.length >= 2}<p class="t-help">{t('tiles.hiddenEverywhere')}</p>{/if}
				{#if fieldError('hidden_on')}<p class="field-error" role="alert">{fieldError('hidden_on')}</p>{/if}
			</div>

			<QuickAccessSection entityId={entity.entity_id} {name} />

			<details class="tech">
				<summary>{t('tiles.request')}</summary>
				<pre data-testid="tile-request">PUT /api/areas/{area.id}/entities/{entity.entity_id}
{request}</pre>
			</details>
		</div>

		<footer class="pfoot">
			<button type="button" class="btn btn-ghost danger" onclick={reset} disabled={saving || !Object.keys(entity.tile || {}).some((k) => k !== 'icon_svg' && k !== 'secondary_state')} data-testid="tile-reset">{t('tiles.reset')}</button>
			<span class="flex-1"></span>
			<button type="button" class="btn" onclick={cancel} disabled={saving}>{t('common.cancel')}</button>
			<button type="button" class="btn btn-primary" onclick={save} disabled={!dirty || saving} data-testid="tile-save">{saving ? t('common.saving') : t('common.save')}</button>
		</footer>
	</div>
{/if}

<style>
	.tpanel {
		display: flex;
		flex-direction: column;
		background: var(--surface);
		border: 1px solid var(--line);
		border-radius: var(--r-card);
		max-height: calc(100dvh - 24px);
		position: sticky;
		top: 12px;
		min-width: 0;
	}
	.tpanel.sheet {
		position: fixed;
		inset: 0;
		z-index: 200;
		max-height: none;
		border-radius: 0;
		border: 0;
		padding-bottom: env(safe-area-inset-bottom);
	}
	.phead {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 12px 14px;
		border-bottom: 1px solid var(--line);
	}
	.phead h2 {
		margin: 0;
		font-size: var(--fs-t3);
		font-weight: 700;
		overflow-wrap: anywhere;
	}
	.phead h2:focus-visible {
		outline: 2px solid var(--focus);
		outline-offset: 2px;
		border-radius: 4px;
	}
	.phead small {
		color: var(--ink-3);
		font-size: var(--fs-cap);
	}
	.dirty {
		color: var(--warn-ink);
		font-size: var(--fs-cap);
		font-weight: 650;
	}
	.chipic {
		width: 34px;
		height: 34px;
		border-radius: 10px;
		display: grid;
		place-items: center;
		background: var(--fill-2);
		color: var(--ink-2);
		flex: none;
	}
	.chipic svg {
		width: 20px;
		height: 20px;
	}
	.pbody {
		overflow-y: auto;
		padding: 12px 14px;
		display: grid;
		gap: 14px;
		flex: 1;
		min-height: 0;
	}
	.pv {
		background: var(--surface-2);
		border-radius: var(--r-box);
		padding: 10px;
	}
	.pvtabs {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	@media (pointer: fine) {
		.pvtabs :global(.seg-sm button) {
			min-height: 26px;
			padding: 0 8px;
			font-size: var(--fs-sm);
		}
	}
	.pvsum {
		margin: 4px 0 0;
		text-align: center;
		font-size: var(--fs-sm);
		color: var(--ink-2);
	}
	.grp {
		display: grid;
		gap: 10px;
	}
	.grp h3 {
		margin: 0;
		font-size: var(--fs-cap);
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--ink-3);
	}
	.iconbtn {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		min-height: 48px;
		padding: 6px 10px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-ctl);
		background: var(--surface);
		color: var(--ink);
		text-align: left;
	}
	.iconbtn-text {
		display: grid;
	}
	.iconbtn-text span {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.iconbtn small {
		color: var(--ink-3);
		font-size: var(--fs-cap);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.iconbtn.invalid {
		border-color: var(--err);
	}
	.locked {
		margin: 0;
		padding: 8px 10px;
		border-radius: var(--r-sm);
		background: var(--fill);
		color: var(--ink-2);
		font-size: var(--fs-sm);
	}
	.scope {
		display: flex;
		gap: 6px;
		align-items: flex-start;
		margin: 6px 0 0;
		padding: 8px 10px;
		border-radius: var(--r-sm);
		background: var(--fill);
		font-size: var(--fs-sm);
		color: var(--ink-2);
	}
	.scope.warn {
		background: var(--warn-soft);
		color: var(--warn-ink);
		display: block;
	}
	.scope.err {
		background: var(--err-soft);
		color: var(--err-ink);
	}
	.scope.ok {
		background: var(--ok-soft);
		color: var(--ok-ink);
	}
	.scope :global(.ic) {
		flex: none;
		width: 16px;
		height: 16px;
		vertical-align: -3px;
	}
	.linkish {
		border: 0;
		background: none;
		padding: 0;
		color: inherit;
		font-weight: 650;
		text-decoration: underline;
		cursor: pointer;
	}
	.swatches {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(64px, 1fr));
		gap: 6px;
	}
	.swatch {
		display: grid;
		justify-items: center;
		gap: 4px;
		padding: 6px 2px;
		border: 1px solid transparent;
		border-radius: var(--r-sm);
		background: none;
		color: var(--ink-2);
		font-size: var(--fs-cap);
		min-height: 44px;
	}
	.swatch[aria-checked='true'] {
		border-color: var(--line-2);
		background: var(--fill);
		color: var(--ink);
		font-weight: 650;
	}
	.sw {
		width: 26px;
		height: 26px;
		border-radius: 50%;
		display: grid;
		place-items: center;
		color: #fff;
		box-shadow: inset 0 0 0 1px rgb(0 0 0 / 0.12);
	}
	.sw :global(.ic) {
		width: 16px;
		height: 16px;
	}
	.sw-auto {
		background: conic-gradient(#f5b82e, #f06a2c, #7a5af0, #2f8ceb, #1f9db0, #f5b82e);
	}
	.sw-accent {
		background: #e8643c;
	}
	.sw-light {
		background: #f5b82e;
		color: #3b2a00;
	}
	.sw-heat {
		background: #f06a2c;
	}
	.sw-safe {
		background: #2f9e55;
	}
	.sw-sea {
		background: #1f9db0;
	}
	.sw-cool {
		background: #2f8ceb;
	}
	.sw-media {
		background: #7a5af0;
	}
	.sw-neutral {
		background: #8a8e99;
	}
	.devs {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 16px;
	}
	.chk {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-height: 36px;
	}
	.chk input {
		width: 18px;
		height: 18px;
		accent-color: var(--accent);
	}
	.tech summary {
		cursor: pointer;
		color: var(--ink-3);
		font-size: var(--fs-sm);
	}
	.tech pre {
		margin: 6px 0 0;
		padding: 8px;
		border-radius: var(--r-sm);
		background: var(--fill);
		font-family: var(--mono);
		font-size: 12px;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.pfoot {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 14px;
		border-top: 1px solid var(--line);
	}
	.danger {
		color: var(--err-ink);
	}
	.tpanel :global(.pro) {
		margin: 0;
	}
	@media (pointer: coarse) {
		.chk {
			min-height: 44px;
		}
	}
</style>
