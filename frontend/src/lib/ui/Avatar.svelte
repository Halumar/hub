<script>
	/** Initial of an owner name (decorative; the name is always shown next to it). */
	let { name = '' } = $props();
	let initial = $derived((name || '').trim().charAt(0).toUpperCase());
</script>

<span class="avatar {initial ? '' : 'avatar-none'}" aria-hidden="true">{initial || '–'}</span>
