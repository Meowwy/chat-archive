import adapter from '@sveltejs/adapter-static';

export default {
	kit: {
		// Single-page app: the FastAPI server owns routing for /api and serves
		// this fallback for everything else.
		adapter: adapter({ fallback: 'index.html', strict: false })
	}
};
