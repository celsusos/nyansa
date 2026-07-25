import { defineConfig } from '@quasar/app-vite';

export default defineConfig(() => ({
  boot: [],
  css: ['app.css'],
  extras: ['roboto-font', 'material-icons'],
  build: {
    target: {
      browser: ['es2022', 'firefox115', 'chrome115', 'safari14'],
      node: 'node24'
    },
    typescript: {
      strict: true,
      vueShim: true
    },
    filenameBasedRouting: true,
    vueRouterMode: 'hash',
    sourcemap: true
  },
  devServer: {
    open: false
  },
  framework: {
    config: {
      brand: {
        primary: '#1f6f5b',
        secondary: '#31485f',
        accent: '#b8872d',
        dark: '#20252b',
        positive: '#1f7a4d',
        negative: '#b42318',
        info: '#2f6f9f',
        warning: '#a36200'
      }
    },
    plugins: ['Notify']
  },
  animations: [],
  ssr: {
    prodPort: 3000,
    middlewares: ['render'],
    pwa: false
  },
  pwa: {
    workboxMode: 'GenerateSW'
  },
  cordova: {},
  capacitor: {
    hideSplashscreen: true
  },
  electron: {
    preloadScripts: ['electron-preload'],
    inspectPort: 5858,
    bundler: 'packager',
    packager: {},
    builder: {
      appId: 'agent-ui'
    }
  },
  bex: {
    extraScripts: []
  }
}));
