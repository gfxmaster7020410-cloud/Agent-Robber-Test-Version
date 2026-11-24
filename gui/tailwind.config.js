/** @type {import('tailwindcss').Config} */
export default {
	content: ['./index.html', './src/**/*.{ts,tsx}'],
	corePlugins: {
		preflight: false,
	},
	theme: {
		extend: {
			colors: {
				onyx: '#0D0D10',
				panel: '#16161C',
				ink: '#1E1E26',
				amber: '#FF8C00',
				line: 'rgba(255,255,255,0.08)',
				wb: {
					bg: '#181820',
					card: '#232330',
					border: '#2d2d3d',
					green: '#22c55e',
					'green-text': '#50fa7b',
					red: '#ef4444',
					'red-text': '#ff6b6b',
					yellow: '#f1fa8c',
					muted: '#8b8b9a',
					fg: '#e8e8ed',
				},
			},
			boxShadow: {
				bento: '0 0 0 1px rgba(255,255,255,0.06)',
			},
			fontFamily: {
				sans: ['BaseNeue', '"Clash Display"', 'system-ui', 'sans-serif'],
				mono: ['BogitaMono', '"Bogita Mono"', 'ui-monospace', 'Consolas', 'monospace'],
				display: ['BaseNeue', '"Clash Display"', 'system-ui', 'sans-serif'],
			},
		},
	},
	plugins: [],
};
