/** @type {import('tailwindcss').Config} */
// Meeting-Ops brand palette (Brand-Ops kit): navy + gold "comms officer".
// The app was built on Tailwind's purple/fuchsia/indigo accents. Rather than
// rewrite hundreds of class names, we remap those built-in color NAMES to the
// brand ramps here — so every existing `purple-600` / `fuchsia-500` / `indigo-*`
// usage recolors cohesively to navy/gold in one place. Reversible: delete the
// colors block to restore Tailwind defaults.
const navy = {
  50: '#eef3fb', 100: '#d6e2f5', 200: '#adc5ea', 300: '#7fa3da', 400: '#5181c6',
  500: '#3463ac', 600: '#284f8c', 700: '#1b3a6b', 800: '#142d54', 900: '#0f2240', 950: '#0a1228',
};
const gold = {
  50: '#fdf9ed', 100: '#f9efcc', 200: '#f2dd96', 300: '#e9c65c', 400: '#ddb13f',
  500: '#d4af37', 600: '#b8932a', 700: '#936f22', 800: '#785a22', 900: '#654b20', 950: '#3a2a0f',
};
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // brand-named tokens for new work
        navy,
        gold,
        // remap the accents the app already uses -> brand
        purple: navy,
        violet: navy,
        indigo: navy,
        fuchsia: gold,
        pink: gold,
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        display: ['"Space Grotesk"', 'Inter', 'ui-sans-serif', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
}
