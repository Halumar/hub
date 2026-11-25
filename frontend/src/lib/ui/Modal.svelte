<script>
	/**
	 * Modal dialog on the native <dialog> (top layer, inert background).
	 *
	 * - Focus: moves into the dialog on open ([data-autofocus] > first field >
	 *   first focusable), Tab/Shift+Tab stay inside, focus returns to the
	 *   trigger on close.
	 * - Esc and a click on the backdrop close it (unless `dismissible` is false).
	 * - Phones (< 640 px): bottom sheet.
	 *
	 *   <Modal bind:open title="Edit area" onclose={…}>
	 *     …body…
	 *     {#snippet footer()}<button class="btn">…</button>{/snippet}
	 *   </Modal>
	 */
	import { t } from '$lib/i18n/index.svelte.js';
	import Icon from './Icon.svelte';

	let {
		open = $bindable(false),
		title = '',
		description = '',
		size = 'md',
		dismissible = true,
		closeOnBackdrop = true,
		showClose = true,
		onclose = null,
		testid = undefined,
		panelClass = '',
		bodyClass = '',
		children,
		footer = null,
		header = null,
	} = $props();

	const uid = `m${Math.random().toString(36).slice(2, 9)}`;
	const titleId = `${uid}-title`;
	const descId = `${uid}-desc`;

	const FOCUSABLE =
		'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

	function requestClose() {
		if (!dismissible) return;
		open = false;
		onclose?.();
	}

	function focusables(root) {
		return [...root.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement);
	}

	/** Svelte action: open as modal, trap focus, restore focus on destroy. */
	function modal(dialog) {
		const previous = /** @type {HTMLElement|null} */ (document.activeElement);
		dialog.showModal();

		const panel = dialog.querySelector('.modal-panel');
		const body = dialog.querySelector('.modal-body');
		const target =
			dialog.querySelector('[data-autofocus]') ||
			body?.querySelector('input:not([disabled]):not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), select:not([disabled]), textarea:not([disabled])') ||
			(body && focusables(body)[0]) ||
			focusables(dialog)[0] ||
			panel;
		/** @type {HTMLElement} */ (target)?.focus?.({ preventScroll: true });

		function onCancel(e) {
			// Esc: close through our state so the parent's bind:open stays in sync.
			e.preventDefault();
			requestClose();
		}
		function onKeydown(e) {
			if (e.key !== 'Tab') return;
			const items = focusables(dialog);
			if (items.length === 0) {
				e.preventDefault();
				return;
			}
			const first = items[0];
			const last = items[items.length - 1];
			if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
				e.preventDefault();
				last.focus();
			} else if (!e.shiftKey && document.activeElement === last) {
				e.preventDefault();
				first.focus();
			}
		}
		dialog.addEventListener('cancel', onCancel);
		dialog.addEventListener('keydown', onKeydown);

		return {
			destroy() {
				dialog.removeEventListener('cancel', onCancel);
				dialog.removeEventListener('keydown', onKeydown);
				if (dialog.open) dialog.close();
				if (previous && previous.isConnected) previous.focus({ preventScroll: true });
			},
		};
	}

	function onBackdrop(e) {
		if (closeOnBackdrop && e.target === e.currentTarget) requestClose();
	}
</script>

{#if open}
	<dialog
		use:modal
		class="modal"
		aria-labelledby={title ? titleId : undefined}
		aria-describedby={description ? descId : undefined}
		data-testid={testid}
	>
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="modal-wrap" onclick={onBackdrop}>
			<div class="modal-panel modal-{size} {panelClass}">
				{#if header}
					{@render header({ titleId, close: requestClose })}
				{:else if title}
					<div class="modal-head">
						<div class="grow min-w-0 flex-1">
							<h2 id={titleId}>{title}</h2>
							{#if description}<p id={descId} class="t-help mt-1">{description}</p>{/if}
						</div>
						{#if showClose && dismissible}
							<button type="button" class="btn btn-ghost btn-icon btn-sm" onclick={requestClose} aria-label={t('common.close')}>
								<Icon name="x" />
							</button>
						{/if}
					</div>
				{/if}
				<div class="modal-body {bodyClass}">
					{@render children?.()}
				</div>
				{#if footer}
					<div class="modal-foot">
						{@render footer({ close: requestClose })}
					</div>
				{/if}
			</div>
		</div>
	</dialog>
{/if}
