// ReDoc start-up (kept out of the page: the CSP allows no inline script).
window.addEventListener('DOMContentLoaded', function () {
	Redoc.init(new URL('openapi.json', document.baseURI).href, { hideDownloadButton: false }, document.getElementById('redoc'));
});
