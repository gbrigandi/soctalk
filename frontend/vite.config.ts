import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		proxy: {
			// Proxy API requests to FastAPI backend
			'/api': {
				target: process.env.API_URL || 'http://127.0.0.1:8000',
				changeOrigin: true
			}
		}
	}
});
