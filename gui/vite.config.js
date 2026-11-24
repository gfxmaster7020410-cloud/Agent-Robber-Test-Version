import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

const tauri = Boolean(process.env.TAURI_ENV_PLATFORM);

export default defineConfig({
	plugins: [react()],
	clearScreen: false,
	envPrefix: ['VITE_', 'TAURI_ENV_', 'TAURI_'],
	base: tauri ? './' : '/',
	worker: {format: 'es'},
	optimizeDeps: {
		include: ['monaco-editor', '@monaco-editor/react'],
	},
	server: {
		host: '127.0.0.1',
		port: 5173,
		strictPort: true,
		hmr: {host: '127.0.0.1'},
		watch: {ignored: ['**/src-tauri/**']},
		proxy: {
			'/api': {target: 'http://127.0.0.1:8787', changeOrigin: true},
		},
	},
	preview: {
		host: '127.0.0.1',
		port: 4173,
		strictPort: true,
	},
});
