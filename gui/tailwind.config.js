/** @type {import('tailwindcss').Config} */
export default {
	content: ['./index.html', './src/**/*.{ts,tsx}'],
	theme: {
		extend: {
			colors: {
				onyx: '#0D0D10',
				panel: '#16161C',
				ink: '#1E1E26',
				amber: '#FF8C00',
				line: 'rgba(255,255,255,0.08)',
			},
			boxShadow: {
				bento: '0 0 0 1px rgba(255,255,255,0.06)',
			},
			fontFamily: {
				sans: ['"Clash Display"', 'system-ui', 'sans-serif'],
				mono: ['"Bogita Mono"', 'ui-monospace', 'Consolas', 'monospace'],
				display: ['"Clash Display"', 'system-ui', 'sans-serif'],
			},
		},
	},
	plugins: [],
};
