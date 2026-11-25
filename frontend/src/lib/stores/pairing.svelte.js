/**
 * Global state of the pairing dialog, so it can be opened from anywhere
 * (overview, devices, guests) and is mounted once in the layout.
 */
let _open = $state(false);
let _role = $state('member');
// Stay on the current page after pairing (setup assistant) instead of opening the device.
let _stay = $state(false);

export const pairingDialog = {
	get open() {
		return _open;
	},
	set open(v) {
		_open = v;
	},
	get role() {
		return _role;
	},
	set role(v) {
		_role = v;
	},
	get stay() {
		return _stay;
	},
};

/**
 * @param {'member'|'guest'|'shared'} [role]
 * @param {{ stay?: boolean }} [options] stay: do not open the device page after pairing
 */
export function openPairing(role = 'member', { stay = false } = {}) {
	_role = role;
	_stay = stay;
	_open = true;
}

export function closePairing() {
	_open = false;
}
