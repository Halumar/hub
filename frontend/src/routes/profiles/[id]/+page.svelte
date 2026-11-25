<script>
	/**
	 * Profile editor: name, parental controls, then every rule of the
	 * catalog by group – value, checkbox "Fixed" (not adjustable on devices)
	 * and, for comfort rules, "User's choice". Boundaries are shown as a
	 * readable line with lock and "Why?". Saving always shows the preview
	 * (?dry_run=true): affected devices, adjustments that are dropped, HA
	 * keys, loosened security rules (confirmation).
	 */
	import { onMount, untrack } from 'svelte';
	import { page } from '$app/state';
	import { base } from '$app/paths';
	import { goto, beforeNavigate } from '$app/navigation';
	import { api } from '$lib/api.js';
	import { addToast } from '$lib/stores/toast.svelte.js';
	import { t, i18n } from '$lib/i18n/index.svelte.js';
	import { formatDate } from '$lib/utils.js';
	import PageHeader from '$lib/ui/PageHeader.svelte';
	import Icon from '$lib/ui/Icon.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Modal from '$lib/ui/Modal.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import Field from '$lib/ui/Field.svelte';
	import LoadingState from '$lib/ui/LoadingState.svelte';
	import EmptyState from '$lib/ui/EmptyState.svelte';
	import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
	import ProfileIcon from '$lib/components/profiles/ProfileIcon.svelte';
	import RuleValueEditor from '$lib/components/profiles/RuleValueEditor.svelte';
	import PreviewView from '$lib/components/profiles/PreviewView.svelte';
	import LooseningWarning from '$lib/components/profiles/LooseningWarning.svelte';
	import DeleteProfileDialog from '$lib/components/profiles/DeleteProfileDialog.svelte';
	import RemotePinDialog from '$lib/components/profiles/RemotePinDialog.svelte';
	import ProfileHistory from '$lib/components/profiles/ProfileHistory.svelte';
	import SceneHints from '$lib/components/profiles/SceneHints.svelte';
	import RemoteKeyWarning from '$lib/components/profiles/RemoteKeyWarning.svelte';
	import { hintsForDraft } from '$lib/profiles/scene-hints.js';
	import { loadCatalog, groupsWithRules, L, enforcementOf, boundaryOf, boundaryInfo, reasonText, limits, controlPathOf, KEY_PLATFORMS } from '$lib/profiles/catalog.svelte.js';
	import { clone, formatValue, kindLabel, profileKinds, profileSummary, same, textOf } from '$lib/profiles/format.js';
	import { loadRuleContext, areaNamer, areaTagger, kidsRulesOf } from '$lib/profiles/context.js';
	import { errorText } from '$lib/profiles/errors.js';
	import { platformsSummary, shownPlatforms } from '$lib/device-info.js';

	/** Profile kinds whose devices always hold their own key: "Hub only" is not offered. */
	const KEY_KINDS = ['tv'];
	const TV_PLATFORMS = ['tvos', 'android_tv'];
	// Platforms named in the hint: those without an app are not listed (like the default profiles).
	const SHOWN_KEY_PLATFORMS = shownPlatforms(KEY_PLATFORMS);

	let profile = $state(null);
	let profiles = $state([]);
	let clients = $state([]);
	let cat = $state(null);
	let ref = $state({ areas: [], tags: [], entities: [] });
	let loading = $state(true);
	let loadError = $state('');
	let notFound = $state(false);

	let draft = $state(null);
	let query = $state('');
	let jump = $state('');

	let previewOpen = $state(false);
	let preview = $state(null);
	let previewBusy = $state(false);
	let previewError = $state('');
	let confirmed = $state(false);
	let saving = $state(false);

	let updateOpen = $state(false);
	let updatePreview = $state(null);
	let updateBusy = $state(false);
	let updateConfirmed = $state(false);

	let deleteOpen = $state(false);
	let pinOpen = $state(false);
	let pinBusy = $state(false);
	let leaveOpen = $state(false);
	let leaveTo = null;
	let whyOpen = $state({});

	let id = $derived(page.params.id);
	/** Suggestion id → name (text object) for the origin line. */
	let suggestionNames = $state({});

	onMount(() => {
		loadRuleContext().then((r) => (ref = r));
	});

	$effect(() => {
		const pid = id;
		if (pid) untrack(() => load(pid));
	});

	async function load(pid) {
		loading = !profile || profile.id !== pid;
		try {
			const [p, list, c] = await Promise.all([api.getProfile(pid), api.listProfiles(), loadCatalog()]);
			cat = c;
			setProfile(p);
			profiles = list?.profiles || [];
			loadError = '';
			notFound = false;
			api.getPairedClients().then((cl) => (clients = (cl || []).filter((x) => x.profile_id === pid && !x.pending_deletion))).catch(() => {});
			// The profile names its suggestion only by id (contract `origin`); the name comes from the gallery.
			if (p.origin && !suggestionNames[p.origin.suggestion_id]) {
				api.getProfileSuggestions()
					.then((r) => (suggestionNames = Object.fromEntries((r?.suggestions || []).map((x) => [x.id, x.name]))))
					.catch(() => {});
			}
			if (page.url.searchParams.get('update') === '1' && p.suggestion_update) openUpdate();
		} catch (e) {
			if (e.status === 404) notFound = true;
			else loadError = e.message;
		} finally {
			loading = false;
		}
	}

	function bodyOf(p) {
		return {
			name: p.name,
			description: p.description || '',
			kind: p.kind || 'person',
			child_protection: !!p.child_protection,
			rules: clone(p.rules || {}),
			fixed: [...(p.fixed || [])].sort(),
			user_choice: [...(p.user_choice || [])].sort(),
		};
	}

	function setProfile(p) {
		profile = p;
		draft = bodyOf(p);
	}

	let baseline = $derived(profile ? bodyOf(profile) : null);
	let dirty = $derived(!!draft && !!baseline && JSON.stringify(normalize(draft)) !== JSON.stringify(normalize(baseline)));
	let catalogDefaults = $derived(profile?.catalog_defaults || []);
	let maxName = $derived(limits().profile_name_max_length || 40);
	let nameValid = $derived(!!draft?.name?.trim() && draft.name.trim().length <= maxName);

	function normalize(b) {
		return { ...b, fixed: [...b.fixed].sort(), user_choice: [...b.user_choice].sort(), description: b.description || '' };
	}

	/** PATCH body with the changed fields only (rules merged per key). */
	function patchBody() {
		const b = {};
		const base0 = baseline;
		if (draft.name.trim() !== base0.name) b.name = draft.name.trim();
		if ((draft.description || '') !== (base0.description || '')) b.description = draft.description.trim() || null;
		if (draft.kind !== base0.kind) b.kind = draft.kind;
		if (draft.child_protection !== base0.child_protection) b.child_protection = draft.child_protection;
		const rules = {};
		for (const [k, v] of Object.entries(draft.rules)) if (!same(v, base0.rules[k]) || catalogDefaults.includes(k)) rules[k] = v;
		if (Object.keys(rules).length) b.rules = rules;
		if (!same([...draft.fixed].sort(), base0.fixed)) b.fixed = [...draft.fixed].sort();
		if (!same([...draft.user_choice].sort(), base0.user_choice)) b.user_choice = [...draft.user_choice].sort();
		return b;
	}

	let groups = $derived.by(() => {
		if (!cat) return [];
		const q = query.trim().toLowerCase();
		return groupsWithRules(cat)
			.map((g) => ({
				...g,
				rules: q ? g.rules.filter((r) => `${L(r.label)} ${L(r.help)} ${L(g.label)}`.toLowerCase().includes(q)) : g.rules,
			}))
			.filter((g) => g.rules.length);
	});

	/** Effective control path of the draft (no stored value: parental controls mean "Hub only"). */
	let path = $derived(draft ? controlPathOf(draft) : 'checked');
	let keyKind = $derived(KEY_KINDS.includes(draft?.kind));
	// Guest devices of a profile without a stored level are on "Hub only" whatever its kind.
	let levelStored = $derived(!(profile?.catalog_defaults || []).includes('control_path') || !same(draft?.rules?.control_path, baseline?.rules?.control_path));
	let guestOnHub = $derived(!!profile?.guest_device_count && !levelStored);
	/** Parental controls just switched on and with them "Hub only" (a visible change). */
	let childHubSet = $derived(!!draft?.child_protection && !baseline?.child_protection && path === 'hub' && baseline?.rules?.control_path !== 'hub');

	let ctx = $derived({
		...ref,
		areaName: areaNamer(ref.areas),
		childProtection: !!draft?.child_protection,
		kidsRules: kidsRulesOf(draft?.rules),
		hiddenChoices: keyKind ? { control_path: ['hub'] } : {},
	});

	/** "Apple TV, Android TV, Apple Watch or Wear OS": the platforms that never run without a key. */
	function platformList(platforms) {
		const names = platforms.map((p) => t(`profiles.slots.${p}`));
		try {
			return new Intl.ListFormat(i18n.locale, { type: 'disjunction' }).format(names);
		} catch {
			return names.join(', ');
		}
	}

	/** Scenes and scripts of the profile with targets it may not switch (saved hints, adjusted to the draft areas). */
	let sceneHints = $derived(profile && draft ? hintsForDraft(profile.scene_hints, draft.rules.areas, areaTagger(ref.areas)) : []);

	/** "Add area" of a scene hint: the area joins the unsaved areas rule. */
	function addArea(areaId) {
		const cur = draft.rules.areas || { mode: 'selected', area_ids: [], tags: [], own_area: false };
		if (cur.mode === 'all' || cur.area_ids?.includes(areaId)) return;
		setValue({ key: 'areas' }, { ...cur, mode: 'selected', area_ids: [...(cur.area_ids || []), areaId] });
	}

	function isFixed(rule) {
		return !rule.fixed_checkbox || draft.fixed.includes(rule.key);
	}

	function setFixed(rule, on) {
		draft.fixed = on ? [...new Set([...draft.fixed, rule.key])] : draft.fixed.filter((k) => k !== rule.key);
	}

	function setUserChoice(rule, on) {
		draft.user_choice = on ? [...new Set([...draft.user_choice, rule.key])] : draft.user_choice.filter((k) => k !== rule.key);
	}

	function setValue(rule, v) {
		draft.rules = { ...draft.rules, [rule.key]: v };
	}

	/** Boundary of parental controls that decides the rule (cap = strictest). */
	/** The parental-controls boundary of a rule, unless the kids rule named in
	 * lifted_by is on (catalog 9: watch with kids_watch). */
	function childBoundary(rule) {
		if (!draft?.child_protection) return null;
		const b = boundaryOf(rule, 'child_protection');
		if (b?.lifted_by && draft.rules[b.lifted_by] === true && draft.rules.remote_access === true) return null;
		return b;
	}

	/** The "Hub only" boundary of a rule (quick_access, watch): active on the hub path unless remote access lifts it. */
	function hubBoundary(rule) {
		if (path !== 'hub') return null;
		const b = boundaryOf(rule, 'hub_only');
		if (b?.lifted_by && draft.rules[b.lifted_by] === true) return null;
		return b;
	}

	/** A boundary whose cap is the strictest value decides the rule (shown read-only with a lock line). */
	function childDecides(rule) {
		for (const b of [childBoundary(rule), hubBoundary(rule)]) {
			if (b && Array.isArray(rule.order) && same(b.cap, rule.order[0])) return b;
		}
		return null;
	}

	/** A TV profile never stays on "Hub only": TVs never run without a key. */
	function onKind() {
		if (KEY_KINDS.includes(draft.kind) && path === 'hub') draft.rules = { ...draft.rules, control_path: 'checked' };
	}

	/** Parental controls on: the profile goes to "Hub only"; off again: back to its saved path. */
	function setChild(on) {
		draft.child_protection = on;
		// A TV never runs without a key: a TV profile keeps "Checked" (or "Direct", capped to "Checked").
		if (on && keyKind) {
			if (path === 'hub') draft.rules = { ...draft.rules, control_path: 'checked' };
			return;
		}
		if (on && path !== 'hub') draft.rules = { ...draft.rules, control_path: 'hub' };
		if (!on && baseline && !baseline.child_protection) draft.rules = { ...draft.rules, control_path: clone(baseline.rules.control_path) };
	}

	function childCap(rule) {
		const b = childBoundary(rule);
		return b && !childDecides(rule) ? b : null;
	}

	function guestCap(rule) {
		if (!profile?.guest_device_count) return null;
		return boundaryOf(rule, 'guest');
	}

	function platformsText(rule) {
		return platformsSummary(rule.platforms);
	}

	// The preview belongs to the write it was asked for: a late answer for an older draft is dropped,
	// and saving needs the preview of exactly the current write.
	let previewSeq = 0;
	let previewFor = $state('');
	let previewCurrent = $derived(!!preview && !!draft && !!baseline && previewFor === JSON.stringify(patchBody()));

	async function openPreview() {
		if (!nameValid) return;
		const body = patchBody();
		const s = ++previewSeq;
		previewOpen = true;
		preview = null;
		previewError = '';
		confirmed = false;
		previewBusy = true;
		try {
			const pv = await api.updateProfile(profile.id, body, { dryRun: true });
			if (s !== previewSeq) return;
			preview = pv;
			previewFor = JSON.stringify(body);
		} catch (e) {
			if (s === previewSeq) previewError = errorText(e);
		} finally {
			if (s === previewSeq) previewBusy = false;
		}
	}

	async function save() {
		saving = true;
		previewError = '';
		try {
			const p = await api.updateProfile(profile.id, patchBody());
			setProfile(p);
			previewOpen = false;
			addToast(t('profiles.editor.saved', { name: p.name }), 'success');
		} catch (e) {
			previewError = errorText(e);
		} finally {
			saving = false;
		}
	}

	function discard() {
		draft = bodyOf(profile);
	}

	async function openUpdate() {
		updateOpen = true;
		updatePreview = null;
		updateConfirmed = false;
		updateBusy = true;
		try {
			updatePreview = await api.applySuggestionUpdate(profile.id, { dryRun: true });
		} catch (e) {
			updateOpen = false;
			addToast(errorText(e), 'error');
		} finally {
			updateBusy = false;
		}
	}

	async function applyUpdate() {
		updateBusy = true;
		try {
			const p = await api.applySuggestionUpdate(profile.id);
			setProfile(p);
			updateOpen = false;
			addToast(t('profiles.editor.updateApplied'), 'success');
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			updateBusy = false;
		}
	}

	async function duplicate() {
		try {
			const p = await api.duplicateProfile(profile.id);
			addToast(t('profiles.editor.duplicated', { name: p.name }), 'success');
			goto(`${base}/profiles/${p.id}`);
		} catch (e) {
			addToast(errorText(e), 'error');
		}
	}

	/** PIN writes keep unsaved rule changes: only the PIN fields of the profile are taken over. */
	function pinFrom(p) {
		profile = { ...profile, has_remote_pin: !!p?.has_remote_pin, remote_pin_fixed: !!p?.remote_pin_fixed, rev: p?.rev ?? profile.rev, updated_at: p?.updated_at ?? profile.updated_at };
	}

	async function savePin(pin) {
		pinFrom(await api.updateProfile(profile.id, { remote_pin: pin }));
		addToast(t('profiles.pin.profileSaved'), 'success');
	}

	async function removePin() {
		pinBusy = true;
		try {
			pinFrom(await api.updateProfile(profile.id, { remote_pin: null }));
			addToast(t('profiles.pin.profileRemoved'), 'success');
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			pinBusy = false;
		}
	}

	async function setPinFixed(on) {
		pinBusy = true;
		try {
			pinFrom(await api.updateProfile(profile.id, { remote_pin_fixed: on }));
			addToast(on ? t('profiles.pin.fixedOn') : t('profiles.pin.fixedOff'), 'success');
		} catch (e) {
			addToast(errorText(e), 'error');
		} finally {
			pinBusy = false;
		}
	}

	beforeNavigate((nav) => {
		if (dirty && !saving && nav.to && nav.type !== 'leave') {
			nav.cancel();
			leaveTo = nav.to.url;
			leaveOpen = true;
		}
	});

	function leave() {
		draft = bodyOf(profile);
		if (leaveTo) goto(leaveTo);
	}

	function onJump(e) {
		const gid = e.currentTarget.value;
		jump = '';
		document.getElementById(`g-${gid}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}

	let originText = $derived.by(() => {
		if (!profile) return '';
		const parts = [];
		if (profile.origin) {
			const sname = suggestionNames[profile.origin.suggestion_id];
			// Until the gallery has answered (or if it fails) the line names no suggestion rather than its id.
			if (sname) parts.push(t('profiles.editor.origin', { name: textOf(sname), version: profile.origin.version }));
		}
		parts.push(t('devices.count', { count: profile.device_count || 0 }));
		parts.push(t('profiles.editor.changed', { date: formatDate(profile.updated_at), by: profile.updated_by || '–' }));
		return parts.join(' · ');
	});
</script>

{#snippet ruleRow(rule)}
	{@const decides = childDecides(rule)}
	{@const ccap = childCap(rule)}
	{@const gcap = guestCap(rule)}
	{@const fixed = isFixed(rule)}
	{@const enf = enforcementOf(rule, path)}
	{@const remoteKey = (rule.key === 'remote_access' || rule.key === 'control_path') && (path === 'hub' || guestOnHub) && draft.rules.remote_access === true}
	{@const wasFixed = !rule.fixed_checkbox || baseline.fixed.includes(rule.key)}
	<div class="rrow" id="r-{rule.key}" data-testid="rule-{rule.key}">
		<div class="rinfo">
			<h4 class="rname">
				{L(rule.label)}
				{#if rule.pro}<span class="badge badge-accent">{t('profiles.pro')}</span>{/if}
				{#if catalogDefaults.includes(rule.key)}<span class="badge badge-info">{t('profiles.editor.newRule')}</span>{/if}
			</h4>
			{#if rule.help}<p class="t-help" data-testid="help-{rule.key}">{L(rule.help)}</p>{/if}
			<div class="meta">
				<span class="badge">{t('profiles.appliesTo', { platforms: platformsText(rule) })}</span>
				{#if enf}
					<span class="badge enf enf-{enf.id}" title={enf.help} data-testid="enf-{rule.key}"><Icon name={enf.id === 'app' ? 'info' : 'shieldcheck'} />{enf.label}<span class="sr-only">: {enf.help}</span></span>
				{/if}
			</div>
		</div>
		<div class="rval">
			{#if rule.profile_kinds && !rule.profile_kinds.includes(draft.kind)}
				<p class="lockline" data-testid="kind-only-{rule.key}">
					<Icon name="lock" size="14" />
					<span>{reasonText('requires_profile_kind', { kinds: rule.profile_kinds.map((k) => kindLabel(k)).join(', ') }) || t('profiles.kind.onlyPerson')}</span>
				</p>
			{:else if decides}
				<p class="ro"><b>{formatValue(rule.key, decides.cap, ctx)}</b></p>
				<p class="lockline" data-testid="decided-{rule.key}" data-reason={decides.reason}>
					<Icon name="lock" size="14" />
					<span>{reasonText(decides.reason)}</span>
					{#if decides.boundary === 'hub_only'}
						<button type="button" class="link why" aria-expanded={whyOpen[rule.key] ? 'true' : 'false'} onclick={() => (whyOpen = { ...whyOpen, [rule.key]: !whyOpen[rule.key] })}>{t('profiles.why')}</button>
					{/if}
				</p>
				{#if whyOpen[rule.key]}<p class="t-help" data-testid="why-{rule.key}">{L(boundaryInfo(decides.boundary)?.effects)}</p>{/if}
			{:else}
				<RuleValueEditor {rule} value={rule.key === 'control_path' ? path : draft.rules[rule.key]} {ctx} idPrefix="p-{rule.key}" onchange={(v) => setValue(rule, v)} />
				{#if rule.key === 'control_path'}
					{#if childHubSet}
						<p class="t-help warnline" data-testid="child-hub-set"><Icon name="child" size="13" />{t('profiles.editor.childHub')}</p>
					{/if}
					{#if keyKind}
						<p class="t-help" data-testid="platform-key-tv">{reasonText('platform_needs_key', { platform: platformList(TV_PLATFORMS) })}</p>
					{:else if path === 'hub'}
						<p class="t-help" data-testid="platform-key">{reasonText('platform_needs_key', { platform: platformList(SHOWN_KEY_PLATFORMS) })}</p>
					{/if}
				{/if}
				{#if rule.key === 'setup_devices' && path === 'hub' && draft.rules.setup_devices?.enabled}
					<p class="t-help" data-testid="setup-hub">{draft.rules.remote_access === true ? t('profiles.editor.setupHubRemote') : t('profiles.editor.setupHub')}</p>
				{/if}
				{#if remoteKey}
					<RemoteKeyWarning variant="profile" testid={rule.key === 'remote_access' ? 'remote-key-warning-profile' : 'remote-key-warning-path'} />
				{/if}
				{#if ccap}
					<p class="lockline"><Icon name="child" size="14" /><span>{t('profiles.editor.childCap', { value: formatValue(rule.key, ccap.cap, ctx) })}</span></p>
				{/if}
				{#if gcap}
					<p class="lockline"><Icon name="users" size="14" /><span>{t('profiles.editor.guestCap', { value: formatValue(rule.key, gcap.cap, ctx) })}</span></p>
				{/if}
				{#if rule.key === 'kids_watch'}
					<p class="t-help" data-testid="kids-watch-needs-remote">{t('profiles.editor.kidsWatchNeedsRemote')}</p>
				{/if}
				{#if rule.key === 'remote_control' && !draft.rules.remote_access}
					<p class="t-help">{t('profiles.editor.remoteNeedsAccess')}</p>
				{/if}
				{#if rule.key === 'access_requests' && draft.rules.access_requests && !draft.rules.access_hours?.enabled}
					<p class="t-help" data-testid="access-requests-need-hours">{t('profiles.editor.accessRequestsNeedHours')}</p>
				{/if}
				{#if rule.key === 'location_required' && (draft.rules.location ?? 'off') === 'off'}
					<p class="t-help" data-testid="location-needs-sharing">{t('profiles.editor.locationNeedsSharing')}</p>
				{/if}
				{#if rule.key === 'home_header' && draft.rules.header === false}
					<p class="t-help" data-testid="home-header-needs-header">{t('profiles.home.headerOff')}</p>
				{/if}
				<p class="t-help status" data-testid="status-{rule.key}">
					{#if draft.user_choice.includes(rule.key)}
						<Icon name="user" size="13" />{t('profiles.editor.userChoiceStatus')}
					{:else if fixed}
						<Icon name="lock" size="13" />{t('profiles.editor.sameEverywhere', { count: profile.device_count || 0 })}
					{:else}
						<Icon name="sliders" size="13" />{t('profiles.editor.adjustable')}
					{/if}
				</p>
				{#if fixed && !wasFixed && profile.adjusted_device_count}
					<p class="t-help warnline" data-testid="fixed-drops-{rule.key}"><Icon name="warn" size="13" />{t('profiles.editor.fixedDrops')}</p>
				{/if}
			{/if}
		</div>
		<div class="rfix">
			{#if rule.fixed_checkbox}
				<label class="check">
					<input type="checkbox" checked={fixed} disabled={!!decides} onchange={(e) => setFixed(rule, e.currentTarget.checked)} data-testid="fixed-{rule.key}" />
					<span><b>{t('profiles.fixed')}</b></span>
				</label>
				<small class="t-help">{t('profiles.fixedHint')}</small>
			{:else}
				<small class="t-help" data-testid="always-fixed-{rule.key}"><Icon name="lock" size="13" /> {t('profiles.alwaysFixed')}</small>
			{/if}
			{#if rule.user_choice_allowed}
				<label class="check mt-1">
					<input type="checkbox" checked={draft.user_choice.includes(rule.key)} onchange={(e) => setUserChoice(rule, e.currentTarget.checked)} data-testid="uc-{rule.key}" />
					<span>{t('profiles.userChoice')}</span>
				</label>
			{/if}
		</div>
	</div>
	{#if rule.key === 'app_settings'}
		{@render pinRow()}
	{/if}
	{#if rule.key === 'areas' && sceneHints.length}
		<div class="hintrow"><SceneHints hints={sceneHints} areaName={areaNamer(ref.areas)} onadd={addArea} testid="scene-hints" /></div>
	{/if}
{/snippet}

{#snippet pinRow()}
	<div class="rrow" id="r-remote-pin" data-testid="rule-remote-pin">
		<div class="rinfo">
			<h4 class="rname">{t('profiles.pin.profileTitle')}</h4>
			<p class="t-help">{t('profiles.pin.profileHint')}</p>
		</div>
		<div class="rval">
			<p class="ro" data-testid="profile-pin-state">
				{#if profile.has_remote_pin}<Icon name="key" size="14" /> <b>{t('profiles.pin.set')}</b>{:else}<span class="t-muted">{t('profiles.pin.none')}</span>{/if}
			</p>
			<div class="btn2">
				<button type="button" class="btn btn-sm" onclick={() => (pinOpen = true)} disabled={pinBusy} data-testid="profile-pin-set"><Icon name="key" /><span class="truncate">{profile.has_remote_pin ? t('profiles.pin.change') : t('profiles.pin.setBtn')}</span></button>
				<button type="button" class="btn btn-sm" onclick={removePin} disabled={pinBusy || !profile.has_remote_pin} data-testid="profile-pin-remove"><Icon name="trash" /><span class="truncate">{t('profiles.pin.remove')}</span></button>
			</div>
		</div>
		<div class="rfix">
			<label class="check">
				<input type="checkbox" checked={!!profile.remote_pin_fixed} disabled={pinBusy || !profile.has_remote_pin} onchange={(e) => setPinFixed(e.currentTarget.checked)} data-testid="profile-pin-fixed" />
				<span><b>{t('profiles.fixed')}</b></span>
			</label>
			<small class="t-help">{t('profiles.pin.fixedHint')}</small>
		</div>
	</div>
{/snippet}

<div class="page" data-testid="profile-editor">
	{#if loading}
		<LoadingState />
	{:else if notFound || !profile}
		<div class="card">
			<EmptyState icon="template" title={notFound ? t('profiles.editor.notFound') : loadError}>
				<a class="btn" href="{base}/profiles"><Icon name="back" />{t('profiles.title')}</a>
			</EmptyState>
		</div>
	{:else}
		<PageHeader title={profile.name} subtitle={originText} back="{base}/profiles" backLabel={t('profiles.title')} titleTestid="profile-title">
			{#snippet actions()}
				<div class="hbtns">
					<button type="button" class="btn" onclick={duplicate} data-testid="profile-duplicate"><Icon name="copy" /><span class="truncate">{t('profiles.editor.duplicate')}</span></button>
					<button type="button" class="btn btn-danger" onclick={() => (deleteOpen = true)} data-testid="profile-delete"><Icon name="trash" /><span class="truncate">{t('common.delete')}</span></button>
				</div>
			{/snippet}
		</PageHeader>

		{#if profile.suggestion_update}
			<Banner tone="info" icon="sparkle" title={t('profiles.editor.updateTitle', { version: profile.suggestion_update.version })} class="mb-3" testid="suggestion-update">
				{t('profiles.editor.updateText')}
				{#snippet actions()}<button type="button" class="btn btn-sm" onclick={openUpdate} data-testid="see-update">{t('profiles.gallery.seeNews')}</button>{/snippet}
			</Banner>
		{/if}
		{#if catalogDefaults.length}
			<Banner tone="info" class="mb-3" testid="catalog-defaults">{t('profiles.editor.catalogDefaults', { count: catalogDefaults.length })}</Banner>
		{/if}

		<section class="card basics" aria-labelledby="basics-h">
			<h2 id="basics-h" class="sr-only">{t('profiles.editor.basics')}</h2>
			<div class="bgrid">
				<div class="stack">
					<div class="row flex-nowrap items-center gap-3">
						<ProfileIcon icon={profile.icon} color={profile.color} size={44} />
						<Field label={t('profiles.name')} id="pname" class="flex-1" error={nameValid ? '' : t('profiles.nameError', { max: maxName })}>
							{#snippet children({ id: fid, describedby, invalid })}
								<input id={fid} class="input" maxlength={maxName} aria-describedby={describedby} aria-invalid={invalid} bind:value={draft.name} data-testid="profile-name" />
							{/snippet}
						</Field>
					</div>
					<Field label={t('profiles.description')} id="pdesc" hint={t('profiles.descriptionHint')}>
						{#snippet children({ id: fid, describedby })}
							<input id={fid} class="input" maxlength="300" aria-describedby={describedby} bind:value={draft.description} />
						{/snippet}
					</Field>
					<Field label={t('profiles.kind.label')} id="pkind" hint={t('profiles.kind.hint')}>
						{#snippet children({ id: fid, describedby })}
							<select id={fid} class="select" aria-describedby={describedby} bind:value={draft.kind} onchange={onKind} data-testid="profile-kind">
								{#each profileKinds() as k (k.id)}<option value={k.id}>{kindLabel(k.id)}</option>{/each}
							</select>
						{/snippet}
					</Field>
					<div class="childsw">
						<Switch checked={draft.child_protection} onchange={setChild} label={t('profiles.childProtection')} description={t('profiles.childProtectionHint')} testid="child-protection" disabled={!!profile.guest_device_count && !draft.child_protection}>
							<p class="t-help" data-testid="child-protection-pro">{t('profiles.childProtectionPro')}</p>
							{#if profile.guest_device_count && !draft.child_protection}<p class="t-help">{t('profiles.err.guestChild')}</p>{/if}
						</Switch>
						<span class="badge badge-accent" data-testid="child-protection-pro-badge">{t('profiles.pro')}</span>
					</div>
				</div>
				<div class="box sumbox">
					<span class="t-label">{t('profiles.editor.summary')}</span>
					<p data-testid="profile-summary">{profileSummary({ ...profile, ...draft }, ctx)}</p>
				</div>
			</div>
			<p class="legend t-help">
				<span><Icon name="lock" size="13" /> <b>{t('profiles.fixed')}</b> = {t('profiles.fixedHint')}</span>
				<span><Icon name="sliders" size="13" /> {t('profiles.legendAdjustable')}</span>
				<span><Icon name="shieldcheck" size="13" /> {t('profiles.legendEnforced')}</span>
				<span><Icon name="info" size="13" /> {t('profiles.legendApp')}</span>
			</p>
			<label class="search">
				<Icon name="search" />
				<span class="sr-only">{t('profiles.editor.search')}</span>
				<input type="search" bind:value={query} placeholder={t('profiles.editor.searchPlaceholder')} data-testid="rule-search" />
			</label>
		</section>

		<div class="layout">
			<nav class="gnav" aria-label={t('profiles.editor.groups')}>
				<ul>
					{#each groups as g (g.id)}
						<li><a href="#g-{g.id}">{L(g.label)}</a></li>
					{/each}
				</ul>
			</nav>
			<div class="gjump">
				<label class="sr-only" for="gjump">{t('profiles.editor.groups')}</label>
				<select id="gjump" class="select" bind:value={jump} onchange={onJump}>
					<option value="">{t('profiles.editor.jump')}</option>
					{#each groups as g (g.id)}<option value={g.id}>{L(g.label)}</option>{/each}
				</select>
			</div>
			<div class="groups">
				{#if !groups.length}
					<div class="card"><EmptyState compact icon="search" title={t('profiles.editor.noMatch', { query })} /></div>
				{/if}
				{#each groups as g (g.id)}
					<section class="card card-flush grp" id="g-{g.id}" aria-labelledby="gh-{g.id}" data-testid="group-{g.id}">
						<div class="ghead">
							<h3 id="gh-{g.id}" class="t-card">{L(g.label)}</h3>
						</div>
						{#if g.id === 'kids' && !draft.child_protection}
							<div class="gnote">
								<p class="t-help" data-testid="kids-off">{t('profiles.editor.kidsOff')}</p>
								<button type="button" class="btn btn-sm" onclick={() => setChild(true)} disabled={!!profile.guest_device_count}>{t('profiles.editor.kidsTurnOn')}</button>
							</div>
						{:else}
							{#each g.rules as rule (rule.key)}
								{@render ruleRow(rule)}
							{/each}
							{#if g.id === 'kids'}
								<p class="t-help safety" data-testid="safety-{g.id}"><Icon name="shieldcheck" size="14" /><span><b>{t('profiles.safety.note')}</b> {t('profiles.safety.noAlarmSystem')}</span></p>
							{/if}
						{/if}
					</section>
				{/each}
			</div>
		</div>

		{#if clients.length}
			<section class="card mt-4" aria-labelledby="pdev-h" data-testid="profile-devices">
				<div class="card-head"><Icon name="iphone" /><h2 id="pdev-h">{t('profiles.editor.devices')}</h2></div>
				<ul class="list">
					{#each clients as c (c.id)}
						<li><a class="link flex-1" href="{base}/clients/{c.id}">{c.name}</a>{#if c.override_count}<span class="badge badge-warn"><Icon name="sliders" />{t('profiles.adjustedCount', { count: c.override_count })}</span>{/if}</li>
					{/each}
				</ul>
			</section>
		{/if}

		<div class="mt-4"><ProfileHistory profileId={profile.id} ctx={{ areaName: areaNamer(ref.areas) }} {profiles} reload={profile.rev} /></div>

		{#if dirty}
			<div class="savebar" role="region" aria-label={t('profiles.editor.unsaved')} data-testid="save-bar">
				<span class="sbtext"><b>{t('profiles.editor.unsaved')}</b> <span class="t-help">{t('profiles.editor.affectsEstimate', { count: profile.device_count || 0 })}</span></span>
				<div class="sbbtns">
					<button type="button" class="btn" onclick={discard} data-testid="discard">{t('profiles.editor.discard')}</button>
					<button type="button" class="btn btn-primary" onclick={openPreview} disabled={!nameValid} data-testid="save-preview">{t('profiles.editor.saveWithPreview')}</button>
				</div>
			</div>
		{/if}
	{/if}
</div>

<Modal bind:open={previewOpen} title={t('profiles.editor.previewTitle')} description={t('profiles.editor.previewHint')} size="lg" testid="save-preview-dialog" dismissible={!saving}>
	{#if previewBusy}
		<LoadingState />
	{:else if preview}
		<div class="stack">
			<PreviewView {preview} ctx={{ areaName: areaNamer(ref.areas) }} testid="save-preview" />
			<SceneHints hints={preview.scene_hints || []} areaName={areaNamer(ref.areas)} title={t('profiles.sceneHints.previewTitle')} testid="preview-scene-hints" />
			<LooseningWarning rules={preview.loosened_rules || []} bind:confirmed />
		</div>
	{/if}
	{#if preview && !previewBusy && !previewCurrent}
		<Banner tone="warn" class="mt-3" testid="save-preview-stale">
			{t('profiles.editor.previewStale')}
			{#snippet actions()}<button type="button" class="btn btn-sm" onclick={openPreview} data-testid="save-preview-refresh">{t('bulk.previewRefresh')}</button>{/snippet}
		</Banner>
	{/if}
	{#if previewError}<Banner tone="error" class="mt-3">{previewError}</Banner>{/if}
	{#snippet footer({ close })}
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close} disabled={saving}>{t('common.back')}</button>
			<button type="button" class="btn btn-primary" onclick={save} disabled={saving || previewBusy || !previewCurrent || (!!preview.loosened_rules?.length && !confirmed)} data-testid="save-confirm">{saving ? t('common.saving') : t('common.save')}</button>
		</div>
	{/snippet}
</Modal>

<Modal bind:open={updateOpen} title={t('profiles.editor.updateDialog', { version: profile?.suggestion_update?.version || '' })} description={t('profiles.editor.updateDialogHint')} size="lg" testid="update-dialog" dismissible={!updateBusy}>
	{#if updatePreview}
		<div class="stack">
			<PreviewView preview={updatePreview} ctx={{ areaName: areaNamer(ref.areas) }} testid="update-preview" />
			<LooseningWarning rules={updatePreview.loosened_rules || []} bind:confirmed={updateConfirmed} />
		</div>
	{:else}
		<LoadingState />
	{/if}
	{#snippet footer({ close })}
		<div class="foot-eq">
			<button type="button" class="btn" onclick={close} disabled={updateBusy}>{t('profiles.editor.keep')}</button>
			<button type="button" class="btn btn-primary" onclick={applyUpdate} disabled={updateBusy || !updatePreview || (!!updatePreview?.loosened_rules?.length && !updateConfirmed)} data-testid="apply-update">{t('profiles.editor.applyUpdate')}</button>
		</div>
	{/snippet}
</Modal>

{#if profile}
	<DeleteProfileDialog bind:open={deleteOpen} {profile} {profiles} ctx={{ areaName: areaNamer(ref.areas) }} ondeleted={() => goto(`${base}/profiles`)} />
	<RemotePinDialog bind:open={pinOpen} title={t('profiles.pin.profileDialog', { name: profile.name })} description={t('profiles.pin.profileDialogHint')} onsave={savePin} testid="profile-pin" />
{/if}
<ConfirmDialog bind:open={leaveOpen} title={t('profiles.editor.leaveTitle')} message={t('profiles.editor.leaveMessage')} confirmLabel={t('profiles.editor.discard')} danger onconfirm={leave} />

<style>
	.hbtns {
		display: inline-grid;
		grid-auto-flow: column;
		grid-auto-columns: minmax(8.5rem, 1fr);
		gap: 8px;
	}
	.hbtns .btn {
		justify-content: center;
		min-width: 0;
	}
	.basics {
		display: grid;
		gap: 12px;
		margin-bottom: 14px;
	}
	.bgrid {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 14px;
	}
	.sumbox p {
		margin: 4px 0 0;
	}
	.childsw {
		display: flex;
		gap: 8px;
		align-items: flex-start;
	}
	.childsw :global(.switch-row) {
		flex: 1;
	}
	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 16px;
		margin: 0;
	}
	.legend span {
		display: inline-flex;
		gap: 4px;
		align-items: center;
	}
	.search {
		display: flex;
		align-items: center;
		gap: 8px;
		min-height: var(--h-ctl);
		padding: 0 12px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-ctl);
		background: var(--surface);
		color: var(--ink-3);
	}
	.search:focus-within {
		outline: 2px solid var(--focus);
	}
	.search input {
		flex: 1;
		min-width: 0;
		border: 0;
		background: none;
		color: var(--ink);
		font: inherit;
		outline: none;
		min-height: calc(var(--h-ctl) - 2px);
	}
	.layout {
		display: grid;
		grid-template-columns: 172px minmax(0, 1fr);
		gap: 14px;
		align-items: start;
	}
	.gnav {
		position: sticky;
		top: 0;
	}
	.gnav ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		gap: 2px;
	}
	.gnav a {
		display: flex;
		align-items: center;
		min-height: 36px;
		padding: 0 10px;
		border-radius: var(--r-ctl);
		color: var(--ink-2);
		font-weight: 600;
		text-decoration: none;
	}
	.gnav a:hover {
		background: var(--fill);
		color: var(--ink);
	}
	.gjump {
		display: none;
	}
	.groups {
		display: grid;
		gap: 14px;
		min-width: 0;
		padding-bottom: 72px;
	}
	.grp {
		scroll-margin-top: 12px;
	}
	.ghead {
		padding: 12px 16px;
		background: var(--surface-2);
		border-bottom: 1px solid var(--line);
	}
	.ghead h3 {
		margin: 0;
	}
	.gnote {
		padding: 14px 16px;
		display: grid;
		gap: 8px;
		justify-items: start;
	}
	.rrow {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr) 9.5rem;
		gap: 10px 16px;
		padding: 14px 16px;
		border-bottom: 1px solid var(--line);
		scroll-margin-top: 12px;
	}
	.rrow:last-child {
		border-bottom: 0;
	}
	.hintrow {
		padding: 12px 16px;
		border-bottom: 1px solid var(--line);
	}
	.rname {
		margin: 0;
		font-size: var(--fs-body);
		font-weight: 650;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
	}
	.rinfo p {
		margin: 2px 0 0;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin-top: 6px;
	}
	.enf-app {
		background: var(--info-soft);
		color: var(--info-ink);
	}
	.enf-ha,
	.enf-hub {
		background: var(--ok-soft);
		color: var(--ok-ink);
	}
	.rval {
		display: grid;
		gap: 6px;
		align-content: start;
		min-width: 0;
	}
	.ro {
		margin: 0;
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.lockline {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 6px;
		align-items: center;
		margin: 0;
		font-size: var(--fs-sm);
		color: var(--info-ink);
	}
	.lockline > span {
		flex: 1 1 14em;
		min-width: 0;
	}
	.why {
		font-size: var(--fs-cap);
		font-weight: 650;
		background: none;
		border: 0;
		padding: 0;
		min-height: 24px;
		cursor: pointer;
	}
	.status {
		display: flex;
		gap: 4px;
		align-items: center;
		margin: 0;
	}
	.warnline {
		display: flex;
		gap: 4px;
		align-items: center;
		color: var(--warn-ink);
		margin: 0;
	}
	.rfix {
		display: grid;
		gap: 2px;
		align-content: start;
	}
	.btn2 {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
		max-width: 360px;
	}
	.btn2 .btn {
		justify-content: center;
		min-width: 0;
	}
	.safety {
		display: flex;
		gap: 6px;
		align-items: flex-start;
		margin: 0;
		padding: 12px 16px;
		border-top: 1px solid var(--line);
	}
	.safety :global(.ic) {
		flex: none;
		margin-top: 2px;
	}
	.savebar {
		position: sticky;
		bottom: 12px;
		z-index: 5;
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		align-items: center;
		justify-content: space-between;
		margin-top: 12px;
		padding: 10px 14px;
		border: 1px solid var(--line-2);
		border-radius: var(--r-box);
		background: var(--surface);
		box-shadow: 0 6px 24px rgb(0 0 0 / 0.12);
	}
	.sbbtns {
		display: inline-grid;
		grid-auto-flow: column;
		grid-auto-columns: minmax(10rem, 1fr);
		gap: 8px;
	}
	.sbbtns .btn {
		justify-content: center;
	}
	@media (max-width: 1100px) {
		.rrow {
			grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr);
		}
		.rfix {
			grid-column: 2;
		}
	}
	@media (max-width: 1023px) {
		.layout {
			grid-template-columns: minmax(0, 1fr);
		}
		.gnav {
			display: none;
		}
		.gjump {
			display: block;
		}
		.bgrid {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	@media (max-width: 700px) {
		.rrow {
			grid-template-columns: minmax(0, 1fr);
		}
		.rfix {
			grid-column: auto;
		}
		.sbbtns {
			width: 100%;
			grid-auto-columns: minmax(0, 1fr);
		}
		.hbtns {
			grid-auto-columns: minmax(0, 1fr);
		}
	}
</style>
