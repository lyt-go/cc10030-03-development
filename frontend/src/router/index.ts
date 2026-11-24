import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import RegionDensityView from '@/views/RegionDensityView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 区域密度是区域维度与密度估算共用的专题页，固定路径必须排在动态模块路径前。
    { path: '/region_density', name: 'region_density', component: RegionDensityView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
