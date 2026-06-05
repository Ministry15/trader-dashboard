/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          950: '#030306',
          900: '#050508',
          800: '#08080f',
          700: '#0c0c14',
          600: '#10101c',
          500: '#141420',
          line: '#1e1e2d',
          hover: '#1a1a28',
        },
        brand: {
          DEFAULT: '#e8600a',
          light: '#f57019',
          dim: '#b84a06',
        },
        profit: { DEFAULT: '#22c55e', dim: '#16a34a', muted: '#064e3b' },
        loss:   { DEFAULT: '#ef4444', dim: '#dc2626', muted: '#450a0a' },
        warn:   { DEFAULT: '#f59e0b', dim: '#d97706' },
        info:   { DEFAULT: '#60a5fa', dim: '#3b82f6' },
        sol:    { DEFAULT: '#9945ff', dim: '#7c3aed' },
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', 'Consolas', 'ui-monospace', 'SFMono-Regular', 'monospace'],
        sans: ['"Space Grotesk"', 'Inter', 'system-ui', 'sans-serif'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'blink': 'blink 1.4s step-start infinite',
      },
      keyframes: {
        blink: { '0%,100%': { opacity: 1 }, '50%': { opacity: 0.2 } },
      },
    },
  },
  plugins: [],
}
