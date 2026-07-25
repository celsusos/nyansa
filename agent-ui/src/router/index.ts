import {
  createRouter,
  createWebHashHistory,
} from 'vue-router';
import ConsolePage from 'src/pages/index.vue';

export default createRouter({
  scrollBehavior: () => ({ left: 0, top: 0 }),
  routes: [
    { path: '/', component: ConsolePage },
    { path: '/:pathMatch(.*)*', redirect: '/' }
  ],
  history: createWebHashHistory()
});
