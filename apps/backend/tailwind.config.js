/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/web/templates/**/*.html",
    "./app/web/static/js/**/*.js",
  ],
  darkMode: 'class', // Enable dark mode with class strategy
  theme: {
    extend: {
      colors: {
        // Brand colors
        brand: {
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          300: '#7dd3fc',
          400: '#38bdf8',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
          800: '#075985',
          900: '#0c4a6e',
          950: '#082f49',
        },
        // Status colors (monitoring specific)
        status: {
          success: {
            light: '#10b981', // green-500
            DEFAULT: '#059669', // green-600
            dark: '#047857', // green-700
          },
          warning: {
            light: '#f59e0b', // amber-500
            DEFAULT: '#d97706', // amber-600
            dark: '#b45309', // amber-700
          },
          error: {
            light: '#ef4444', // red-500
            DEFAULT: '#dc2626', // red-600
            dark: '#b91c1c', // red-700
          },
          unknown: {
            light: '#6b7280', // gray-500
            DEFAULT: '#4b5563', // gray-600
            dark: '#374151', // gray-700
          },
          info: {
            light: '#3b82f6', // blue-500
            DEFAULT: '#2563eb', // blue-600
            dark: '#1d4ed8', // blue-700
          },
        },
        // Dark mode specific
        dark: {
          bg: {
            primary: '#0f172a',    // slate-900
            secondary: '#1e293b',  // slate-800
            tertiary: '#334155',   // slate-700
          },
          border: '#334155',       // slate-700
          text: {
            primary: '#f1f5f9',    // slate-100
            secondary: '#cbd5e1',  // slate-300
            muted: '#a1afc2',      // ↑ from slate-400 (#94a3b8): nudged one notch lighter so muted
                                   //   text clears WCAG AA 4.5:1 on the lightest surface
                                   //   (bg-dark-bg-tertiary / slate-700) — 4.04:1 → 4.65:1.
          },
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.625rem', { lineHeight: '0.75rem' }],
      },
      spacing: {
        '18': '4.5rem',
        '88': '22rem',
        '100': '25rem',
        '112': '28rem',
        '128': '32rem',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'ping-slow': 'ping 2s cubic-bezier(0, 0, 0.2, 1) infinite',
      },
      transitionDuration: {
        '2000': '2000ms',
        '3000': '3000ms',
      },
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography'),
  ],
}
