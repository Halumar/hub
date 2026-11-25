<script>
	/**
	 * Primary link of a clickable row/card ("stretched link"). Its ::after
	 * covers the nearest `.rowlink` ancestor, so the whole row opens `href`
	 * while it stays a real link (middle/Cmd-click, "open in new tab", screen
	 * readers announce one link with the row's name). Controls in the row go
	 * into a `.rowlink-ctl` wrapper: they sit above the overlay and never
	 * navigate, also on touch. Space activates like Enter.
	 *
	 *   <li class="rowlink">
	 *     <h2><RowLink href="{base}/areas/{id}">{name}</RowLink></h2>
	 *     <div class="rowlink-ctl"><Switch … /><Menu … /></div>
	 *     <span class="rowlink-chev" aria-hidden="true"><Icon name="chev" /></span>
	 *   </li>
	 */
	let { href, class: cls = '', testid = undefined, children, ...rest } = $props();

	function onkeydown(e) {
		if (e.key === ' ' && !e.repeat) {
			e.preventDefault();
			e.currentTarget.click();
		}
	}
</script>

<!-- ontouchstart: lets iOS Safari apply :active (pressed state) on tap. -->
<a {href} class="rowlink-target {cls}" draggable="false" data-testid={testid} {onkeydown} ontouchstart={() => {}} {...rest}>{@render children?.()}</a>
