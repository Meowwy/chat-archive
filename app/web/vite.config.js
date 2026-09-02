import { sveltekit } from '@sveltejs/kit/vite';

export default {
	plugins: [sveltekit()],
	server: {
		// `npm run dev` talks to the Python server running on 8765.
		proxy: { '/api': 'http://127.0.0.1:8765' }
	}
};
