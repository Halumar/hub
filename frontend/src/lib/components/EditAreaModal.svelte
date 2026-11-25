<script>
	/**
	 * The one "Area properties" modal (used by the area list and the area editor).
	 *
	 * - Saves only the fields the user actually changed (PATCH diff), so opening
	 *   and saving never rewrites untouched values (e.g. control_mode null ->
	 *   "full" or a missing color -> #4361ee).
	 * - control_mode "" = inherit the global default (stored as null).
	 * - Image upload/remove act immediately and never submit the form.
	 * - Errors are shown inside the modal.
	 */
	import { api, resolveImageSrc } from '$lib/api.js';
	import IconPicker from '$lib/components/IconPicker.svelte';
	import { copyText } from '$lib/utils.js';
	import { t } from '$lib/i18n/index.svelte.js';
	import Modal from '$lib/ui/Modal.svelte';
	import Field from '$lib/ui/Field.svelte';
	import Banner from '$lib/ui/Banner.svelte';
	import Segmented from '$lib/ui/Segmented.svelte';
	import Switch from '$lib/ui/Switch.svelte';
	import Icon from '$lib/ui/Icon.svelte';

	let {
		open = $bindable(false),
		area,
		/** Called after every successful change (save or image op); parent reloads quietly. */
		onSaved,
	} = $props();

	const DEFAULT_COLOR = '#4361ee';
	const IMAGE_TYPES = [
		{ type: 'card', field: 'card_image_url', src: 'card_image_src' },
		{ type: 'bg_light', field: 'bg_light_url', src: 'bg_light_src' },
		{ type: 'bg_dark', field: 'bg_dark_url', src: 'bg_dark_src' },
	];
	const imgTitle = (type) => t(`areas.form.imageTypes.${type}.title`);

	let current = $state(null); // freshest area data (images change it)
	let form = $state(formFrom({}));
	let initial = {};
	let saving = $state(false);
	let nameError = $state('');
	let error = $state('');
	let imageBusy = $state({ card: false, bg_light: false, bg_dark: false });
	let copied = $state(false);
	let globalControlMode = $state('');

	function formFrom(a) {
		return {
			name: a.name || '',
			icon: a.icon || '',
			description: a.description || '',
			color: a.color || DEFAULT_COLOR,
			grid_cols: a.grid_cols || 2,
			tags: (a.tags || []).join(', '),
			control_mode: a.control_mode || '',
			is_active: a.is_active !== false,
			card_image_url: a.card_image_url || '',
			bg_light_url: a.bg_light_url || '',
			bg_dark_url: a.bg_dark_url || '',
		};
	}

	// Seed the form each time the modal opens.
	let wasOpen = false;
	$effect(() => {
		if (open && !wasOpen && area) {
			current = area;
			form = formFrom(area);
			initial = { ...form };
			nameError = '';
			error = '';
			copied = false;
			api.getDefaultControlMode()
				.then((r) => { globalControlMode = r?.control_mode || ''; })
				.catch(() => { globalControlMode = ''; });
		}
		wasOpen = open;
	});

	function validateName() {
		nameError = form.name?.trim() ? '' : t('areas.form.nameRequired');
		return !nameError;
	}

	function parseTags(v) {
		return v ? v.split(',').map((t) => t.trim()).filter(Boolean) : [];
	}

	/** Only the fields that differ from what the modal was opened with. */
	function changedFields() {
		const patch = {};
		if (form.name !== initial.name) patch.name = form.name.trim();
		if (form.icon !== initial.icon) patch.icon = form.icon || null;
		if (form.description !== initial.description) patch.description = form.description || null;
		if (form.color !== initial.color) patch.color = form.color || null;
		if (form.grid_cols !== initial.grid_cols) patch.grid_cols = form.grid_cols;
		if (parseTags(form.tags).join('\u0000') !== parseTags(initial.tags).join('\u0000')) patch.tags = parseTags(form.tags);
		if (form.control_mode !== initial.control_mode) patch.control_mode = form.control_mode || null;
		if (form.is_active !== initial.is_active) patch.is_active = form.is_active;
		for (const { field } of IMAGE_TYPES) {
			if (form[field] !== initial[field]) patch[field] = form[field] || null;
		}
		return patch;
	}

	async function save() {
		if (!area || saving) return;
		if (!validateName()) return;
		const patch = changedFields();
		if (Object.keys(patch).length === 0) {
			open = false;
			return;
		}
		saving = true;
		error = '';
		try {
			await api.patchArea(area.id, patch);
			open = false;
			await onSaved?.();
		} catch (e) {
			error = t('areas.form.saveFailed', { message: e.message });
		} finally {
			saving = false;
		}
	}

	async function refreshCurrent() {
		try {
			current = await api.getArea(area.id);
		} catch {
			// Preview may be stale; the parent reload below still runs.
		}
		await onSaved?.();
	}

	async function uploadImage(type, event) {
		const input = event.currentTarget;
		const file = input.files?.[0];
		if (!file || !area) return;
		imageBusy = { ...imageBusy, [type]: true };
		error = '';
		try {
			if (type === 'card') await api.uploadCardImage(area.id, file);
			else if (type === 'bg_light') await api.uploadBgLight(area.id, file);
			else await api.uploadBgDark(area.id, file);
			await refreshCurrent();
		} catch (e) {
			error = t('areas.form.uploadFailed', { message: e.message });
		} finally {
			imageBusy = { ...imageBusy, [type]: false };
			input.value = '';
		}
	}

	async function removeImage(type) {
		if (!area) return;
		const { field } = IMAGE_TYPES.find((it) => it.type === type);
		imageBusy = { ...imageBusy, [type]: true };
		error = '';
		try {
			if (current?.[`${type === 'card' ? 'card_image' : type}_path`]) {
				if (type === 'card') await api.deleteCardImage(area.id);
				else if (type === 'bg_light') await api.deleteBgLight(area.id);
				else await api.deleteBgDark(area.id);
			}
			if (initial[field]) await api.patchArea(area.id, { [field]: null });
			form[field] = '';
			initial = { ...initial, [field]: '' };
			await refreshCurrent();
		} catch (e) {
			error = t('areas.form.removeFailed', { message: e.message });
		} finally {
			imageBusy = { ...imageBusy, [type]: false };
		}
	}

	function preview(it) {
		return resolveImageSrc(current?.[it.src]) || form[it.field] || null;
	}

	async function copyId() {
		copied = await copyText(area.id);
		if (copied) setTimeout(() => (copied = false), 1500);
	}

	let modeLabel = $derived(globalControlMode ? t('areas.modes.defaultWith', { mode: t(`areas.modes.${globalControlMode}`) }) : t('areas.modes.default'));
</script>

<Modal bind:open title={t('areas.propertiesTitle')} description={area?.name} size="md" dismissible={!saving} testid="edit-area-modal">
	{#if area}
		<form id="edit-area-form" class="stack" onsubmit={(e) => { e.preventDefault(); save(); }}>
			<div class="row t-help">
				<code class="t-mono select-all">{area.id}</code>
				<button type="button" class="btn btn-ghost btn-sm" onclick={copyId}><Icon name={copied ? 'check' : 'copy'} />{copied ? t('areas.idCopied') : t('areas.copyId')}</button>
			</div>
			{#if error}<Banner tone="error">{error}</Banner>{/if}
			<Field label={t('areas.form.name')} required error={nameError} id="edit-area-name">
				{#snippet children({ id, describedby, invalid })}
					<input {id} class="input" required aria-describedby={describedby} aria-invalid={invalid} bind:value={form.name} oninput={() => nameError && validateName()} onblur={validateName} />
				{/snippet}
			</Field>
			<Field label={t('areas.form.description')} id="edit-area-description">
				{#snippet children({ id })}
					<input {id} class="input" bind:value={form.description} placeholder={t('areas.form.descriptionPlaceholder')} />
				{/snippet}
			</Field>
			<div class="grid grid-cols-[1fr_auto] gap-3">
				<Field label={t('areas.form.icon')} id="edit-area-icon">
					{#snippet children({ id })}
						<IconPicker {id} value={form.icon} onchange={(v) => (form.icon = v)} />
					{/snippet}
				</Field>
				<Field label={t('areas.form.color')} id="edit-area-color">
					{#snippet children({ id })}
						<input {id} type="color" class="input color-input" bind:value={form.color} />
					{/snippet}
				</Field>
			</div>
			<div class="grid sm:grid-cols-2 gap-3">
				<div class="field">
					<span class="field-label">{t('areas.form.grid')}</span>
					<Segmented label={t('areas.form.grid')} options={[2, 3, 4].map((n) => ({ value: n, label: String(n) }))} bind:value={form.grid_cols} />
				</div>
				<Field label={t('areas.controlMode')} hint={t('areas.modeHint')} id="edit-area-control-mode">
					{#snippet children({ id, describedby })}
						<select {id} class="select" aria-describedby={describedby} bind:value={form.control_mode}>
							<option value="">{modeLabel}</option>
							<option value="basic">{t('areas.modes.basic')}</option>
							<option value="advanced">{t('areas.modes.advanced')}</option>
							<option value="full">{t('areas.modes.full')}</option>
						</select>
					{/snippet}
				</Field>
			</div>
			<Field label={t('areas.form.tags')} hint={t('areas.form.tagsHint')} id="edit-area-tags">
				{#snippet children({ id, describedby })}
					<input {id} class="input" aria-describedby={describedby} bind:value={form.tags} placeholder={t('areas.form.tagsPlaceholder')} />
				{/snippet}
			</Field>
			<Switch bind:checked={form.is_active} label={t('areas.form.active')} />

			<section class="stack border-t border-line pt-4" aria-labelledby="edit-area-images">
				<h3 id="edit-area-images" class="t-card">{t('areas.form.images')}</h3>
				{#each IMAGE_TYPES as it (it.type)}
					<div class="field">
						<span class="field-label">{imgTitle(it.type)} <span class="t-help">· {t(`areas.form.imageTypes.${it.type}.desc`)}</span></span>
						{#if preview(it)}
							<div class="img-prev">
								<img src={preview(it)} alt={imgTitle(it.type)} />
								<button type="button" class="btn btn-sm img-remove" onclick={() => removeImage(it.type)} disabled={imageBusy[it.type]} aria-label={t('areas.form.removeImage', { title: imgTitle(it.type) })}>
									<Icon name="trash" />
								</button>
							</div>
						{/if}
						<div class="row flex-nowrap">
							<label class="btn btn-sm file-btn flex-none">
								<Icon name="upload" />{imageBusy[it.type] ? t('areas.form.uploading') : t('areas.form.upload')}
								<input type="file" class="sr-only" accept="image/jpeg,image/png,image/webp,image/gif" onchange={(e) => uploadImage(it.type, e)} disabled={imageBusy[it.type]} />
							</label>
							<input class="input min-w-0" bind:value={form[it.field]} placeholder={t('areas.form.imageUrlPlaceholder')} aria-label={t('areas.form.imageUrl', { title: imgTitle(it.type) })} />
						</div>
					</div>
				{/each}
			</section>
		</form>
	{/if}
	{#snippet footer({ close })}
		<button type="button" class="btn" onclick={close} disabled={saving}>{t('common.cancel')}</button>
		<button type="submit" form="edit-area-form" class="btn btn-primary" disabled={saving}>{saving ? t('common.saving') : t('areas.form.saveChanges')}</button>
	{/snippet}
</Modal>

<style>
	.color-input {
		width: 56px;
		padding: 4px;
	}
	.img-prev {
		position: relative;
		border-radius: var(--r-box);
		overflow: hidden;
		border: 1px solid var(--line);
	}
	.img-prev img {
		display: block;
		width: 100%;
		height: 88px;
		object-fit: cover;
	}
	.img-remove {
		position: absolute;
		top: 6px;
		right: 6px;
	}
</style>
