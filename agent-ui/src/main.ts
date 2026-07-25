import { createApp } from 'vue';
import { createPinia } from 'pinia';
import { Quasar, Notify } from 'quasar';
import '@quasar/extras/material-icons/material-icons.css';
import 'quasar/dist/quasar.css';
import './css/app.css';

import App from './App.vue';
import router from './router';

createApp(App)
  .use(Quasar, {
    plugins: {
      Notify
    },
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
    }
  })
  .use(createPinia())
  .use(router)
  .mount('#app');
