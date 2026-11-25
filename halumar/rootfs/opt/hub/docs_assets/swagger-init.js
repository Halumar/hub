// Swagger UI start-up (kept out of the page: the CSP allows no inline script).
window.addEventListener('DOMContentLoaded', function () {
	window.ui = SwaggerUIBundle({
		url: new URL('openapi.json', document.baseURI).href,
		dom_id: '#swagger-ui',
		deepLinking: true,
		persistAuthorization: false,
		validatorUrl: null,
		presets: [SwaggerUIBundle.presets.apis],
		layout: 'BaseLayout'
	});
});
