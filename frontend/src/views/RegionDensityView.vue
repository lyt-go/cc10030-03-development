<template>
  <section class="density-page" data-testid="region-density">
    <header class="page-head">
      <div>
        <h2>区域密度贡献</h2>
        <p class="page-desc">
          按上级区域汇总各子区的核密度贡献：逐格区分有落点、仅有核贡献与确为零，
          并保留每次对照审批实际采用的网格和结果，旧审批可随时复算。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="seed-demo" type="button" :disabled="busy" @click="seedDemo">
          生成边界演示批次
        </button>
        <button class="btn primary" data-testid="open-approval" type="button" :disabled="busy || !ready" @click="openApproval">
          打开对照审批
        </button>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="reloadBoard">
      <label>
        批次
        <input v-model="batchKey" data-testid="batch-key" placeholder="批次编号" />
      </label>
      <label>
        上级区域
        <select v-model="parentId" data-testid="parent-region">
          <option v-for="item in parentOptions" :key="item.region_id" :value="item.region_id">
            {{ item.name }}
          </option>
        </select>
      </label>
      <label>
        带宽(m)
        <input v-model.number="bandwidth" data-testid="bandwidth" type="number" min="1" step="100" />
      </label>
      <label>
        对照带宽(m)
        <input v-model.number="compareBandwidth" data-testid="compare-bandwidth" type="number" min="1" step="100" />
      </label>
      <button class="btn primary" data-testid="load-board" type="submit" :disabled="busy">读取看板</button>
      <button class="btn" data-testid="compare" type="button" :disabled="busy || !ready" @click="runCompare">
        两种带宽试算
      </button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="notice">{{ notice }}</p>
    <p v-if="busy" class="notice-text">{{ loadingHint }}</p>

    <template v-if="board">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">采用定位点</span>
          <strong class="stat-value" data-testid="sample-total">{{ board.totals.sample_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">参与网格</span>
          <strong class="stat-value">{{ board.totals.grid_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">核贡献合计</span>
          <strong class="stat-value">{{ board.totals.kernel_sum.toFixed(6) }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待补采缺口</span>
          <strong class="stat-value" data-testid="zero-total">{{ board.totals.zero_cells }}</strong>
        </article>
      </div>

      <div class="board-grid">
        <article
          v-for="column in board.columns"
          :key="column.key"
          class="board-column"
          :class="`class-${column.key}`"
          :data-testid="`column-${column.key}`"
        >
          <header>
            <strong>{{ column.label }}</strong>
            <span class="board-count">{{ column.count }} 格</span>
          </header>
          <p class="board-desc">{{ column.description }}</p>
          <ul class="board-cells">
            <li v-for="cell in board.cells[column.key]" :key="cell.grid_geohash">
              <div class="cell-head">
                <code>{{ cell.grid_geohash }}</code>
                <span class="cell-region">{{ cell.owner_region_name }}</span>
              </div>
              <p class="cell-meta">
                落点 {{ cell.sample_count }} · 核贡献 {{ cell.kernel_sum.toFixed(6) }}
                <template v-if="cell.nearest_distance_m !== null"> · 最近来源 {{ cell.nearest_distance_m }}m</template>
              </p>
              <p class="cell-reason">{{ cell.reason }}</p>
              <p v-if="cell.sources.length" class="cell-sources">
                来源：{{ cell.sources.map((item) => `${item.point_key}(${item.weight.toFixed(3)})`).join('、') }}
              </p>
            </li>
            <li v-if="!board.cells[column.key].length" class="empty-state">本带宽下没有此类网格</li>
          </ul>
        </article>
      </div>

      <section class="panel" data-testid="subregion-panel">
        <header class="panel-head">
          <h3>各子区采用明细</h3>
          <span class="panel-note">
            网格 {{ board.totals.grid_count }} 个，跨区网格整格归属交叠面积较大的一侧
          </span>
        </header>
        <table class="data-table">
          <thead>
            <tr>
              <th>子区</th>
              <th>采用点数</th>
              <th>网格数</th>
              <th>有落点</th>
              <th>仅有核贡献</th>
              <th>确为零</th>
              <th>权重合计</th>
              <th>带宽 / 核函数 / 层级</th>
              <th>采用点</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in board.subregions" :key="item.region_id" :data-testid="`subregion-${item.region_id}`">
              <td>{{ item.region_name }}<br /><small>{{ item.region_id }}</small></td>
              <td>{{ item.sample_count }}</td>
              <td>{{ item.grid_count }}</td>
              <td>{{ item.observed_cells }}</td>
              <td>{{ item.kernel_cells }}</td>
              <td>{{ item.zero_cells }}</td>
              <td>{{ item.kernel_sum.toFixed(6) }}</td>
              <td>{{ item.bandwidth }}m / {{ item.kernel }} / {{ item.grid_level }}</td>
              <td class="point-list">
                <span v-for="key in item.sample_points" :key="key">{{ key }}</span>
                <em v-if="!item.sample_points.length">无</em>
              </td>
            </tr>
          </tbody>
        </table>
        <p class="panel-note" data-testid="reconcile">
          父区域合计 {{ board.reconciliation.parent_sample_count }} 点，
          子区合计 {{ board.reconciliation.subregion_sample_total }} 点，
          {{ board.reconciliation.point_sets_match ? '已回对同一批定位点' : '存在未归属点' }}；
          跨区网格 {{ board.reconciliation.cross_boundary_grids.length }} 个。
        </p>
        <ul class="boundary-list">
          <li v-for="grid in board.reconciliation.cross_boundary_grids" :key="grid.grid_geohash">
            <code>{{ grid.grid_geohash }}</code>
            <span v-for="owner in grid.owners" :key="owner.region_id">
              {{ owner.region_id }} 交叠 {{ (owner.overlap_ratio * 100).toFixed(1) }}%
            </span>
          </li>
        </ul>
      </section>

      <section v-if="comparison" class="panel" data-testid="compare-panel">
        <header class="panel-head">
          <h3>两种带宽对照</h3>
          <span class="panel-note">
            {{ comparison.same_sample_set ? '两轮定位点完全一致' : '两轮定位点不一致，请复查输入' }}
          </span>
        </header>
        <div class="compare-grid">
          <article v-for="round in comparison.rounds" :key="round.bandwidth" class="compare-card">
            <h4>带宽 {{ round.bandwidth }}m</h4>
            <p>点数 {{ round.sample_count }} · 有落点 {{ round.observed_cells }} · 仅有核贡献 {{ round.kernel_cells }} · 确为零 {{ round.zero_cells }}</p>
            <table class="data-table compact">
              <thead>
                <tr><th>子区</th><th>点数</th><th>落点格</th><th>核贡献格</th><th>零格</th><th>权重合计</th></tr>
              </thead>
              <tbody>
                <tr v-for="item in round.subregions" :key="item.region_id">
                  <td>{{ item.region_name }}</td>
                  <td>{{ item.sample_count }}</td>
                  <td>{{ item.observed_cells }}</td>
                  <td>{{ item.kernel_cells }}</td>
                  <td>{{ item.zero_cells }}</td>
                  <td>{{ item.kernel_sum.toFixed(6) }}</td>
                </tr>
              </tbody>
            </table>
            <p class="panel-note">
              父区域 {{ round.reconciliation.parent_sample_count }} 点 /
              子区 {{ round.reconciliation.subregion_sample_total }} 点
            </p>
          </article>
        </div>
        <p class="panel-note">{{ comparison.note }}</p>
      </section>
    </template>

    <section class="panel" data-testid="approval-panel">
      <header class="panel-head">
        <h3>对照审批</h3>
        <span class="panel-note">审批打开即冻结输入；确认后仍按当天网格与快照复算</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>审批编号</th>
            <th>批次</th>
            <th>带宽</th>
            <th>网格数</th>
            <th>状态</th>
            <th>处理方式</th>
            <th>迟到点</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in approvals" :key="item.approval_id" :data-testid="`approval-${item.approval_id}`">
            <td>{{ item.label }}<br /><small>{{ item.approval_id }}</small></td>
            <td>{{ item.batch_key }}</td>
            <td>{{ item.bandwidth }}m</td>
            <td>{{ item.grid_count }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.decision || '—' }}</td>
            <td>{{ item.late_points }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="selectApproval(item.approval_id)">详情</button>
              <button class="link" type="button" :disabled="item.status === '已确认'" @click="confirmSelected(item.approval_id)">
                历史对账
              </button>
            </td>
          </tr>
          <tr v-if="!approvals.length">
            <td colspan="8" class="empty-state">还没有对照审批，可先生成演示批次再打开</td>
          </tr>
        </tbody>
      </table>

      <div v-if="approval" class="approval-detail" data-testid="approval-detail">
        <h4>{{ approval.label }} · {{ approval.status }}</h4>
        <p class="panel-note">
          输入 {{ approval.input_summary.sample_count }} 点（{{ approval.input_summary.source }}，
          指纹 {{ approval.input_summary.sample_fingerprint }}），
          采用网格 {{ approval.grid_ids.length }} 个，带宽 {{ approval.bandwidth }}m。
        </p>
        <table class="data-table compact">
          <thead>
            <tr><th>子区</th><th>采用点数</th><th>落点格</th><th>核贡献格</th><th>零格</th><th>权重合计</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in approval.result.subregions" :key="item.region_id">
              <td>{{ item.region_name }}</td>
              <td>{{ item.sample_count }}</td>
              <td>{{ item.observed_cells }}</td>
              <td>{{ item.kernel_cells }}</td>
              <td>{{ item.zero_cells }}</td>
              <td>{{ item.kernel_sum.toFixed(6) }}</td>
            </tr>
          </tbody>
        </table>

        <div class="approval-actions">
          <button class="btn" type="button" @click="verifyDemoLatePoint">接收邻区迟到点</button>
          <button class="btn" type="button" :disabled="approval.status === '已确认'" @click="confirmCorrected">
            采用更正并补采
          </button>
          <button class="btn" type="button" @click="runReplay(false)">按快照复算</button>
          <button class="btn" type="button" @click="runReplay(true)">按当前输入复算</button>
          <button class="btn" type="button" :disabled="!olderApprovalId" @click="runReconcile">
            与旧审批对账
          </button>
        </div>

        <table v-if="approval.late_points.length" class="data-table compact" data-testid="late-points">
          <thead>
            <tr><th>迟到点</th><th>坐标</th><th>核验</th><th>归属子区</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in approval.late_points" :key="item.point_key">
              <td>{{ item.point_key }}</td>
              <td>{{ item.lat.toFixed(5) }}, {{ item.lon.toFixed(5) }}</td>
              <td>{{ item.verification }}</td>
              <td>{{ item.region_name || '—' }}</td>
              <td>{{ item.note || '—' }}</td>
            </tr>
          </tbody>
        </table>

        <label v-if="olderOptions.length" class="older-picker">
          历史合计对账对象
          <select v-model="olderApprovalId" data-testid="older-approval">
            <option value="">选择较早的审批</option>
            <option v-for="item in olderOptions" :key="item.approval_id" :value="item.approval_id">
              {{ item.label }} · {{ item.approval_id }}（{{ item.status }}）
            </option>
          </select>
        </label>

        <div v-if="reconcile" class="replay-result" data-testid="reconcile-result">
          <p>
            旧审批 {{ reconcile.older.approval_id }} 保存 {{ reconcile.older.sample_count }} 点，
            {{ reconcile.history_reproducible ? '可按原点复算' : '复算不一致' }}；
            本次审批 {{ reconcile.current.approval_id }} 保存 {{ reconcile.current.sample_count }} 点。
          </p>
          <p v-if="!reconcile.input_changes.same_sample_set">
            输入变化：新增 {{ reconcile.input_changes.added_points.join('、') || '无' }}；
            移除 {{ reconcile.input_changes.removed_points.join('、') || '无' }}。
          </p>
        </div>

        <div v-if="replay" class="replay-result" data-testid="replay-result">
          <p>
            复算来源：{{ replay.source === 'approval_snapshot' ? '审批输入快照' : '当前已核验输入' }}；
            {{ replay.matches ? '与保存结果一致' : '与保存结果存在差异' }}；
            点数 {{ replay.sample_count }}。
          </p>
          <ul v-if="replay.differences.length">
            <li v-for="(diff, index) in replay.differences" :key="index">
              {{ diff.region_id }}：{{ diff.diff || `${diff.field} 保存 ${diff.stored} / 复算 ${diff.replayed}` }}
            </li>
          </ul>
        </div>

        <div v-if="approval.backfill_plan.length" class="backfill-plan" data-testid="backfill-plan">
          <h5>更正补采计划</h5>
          <ul>
            <li v-for="item in approval.backfill_plan" :key="item.backfill_id">
              {{ item.backfill_id }} · {{ item.region_name }} · {{ item.status }}
            </li>
          </ul>
        </div>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  addDemoLatePoint,
  confirmApproval,
  createDemoBatch,
  getApproval,
  listApprovals,
  loadBandwidthCompare,
  loadDensityBoard,
  loadHierarchy,
  openApproval as openApprovalApi,
  reconcileApprovals,
  replayApproval,
} from '@/api/region-density'
import type {
  ApprovalDetail,
  ApprovalSummary,
  BandwidthCompare,
  DensityBoard,
  ReconcileResult,
  RegionNode,
  ReplayResult,
} from '@/api/region-density'

const batchKey = ref('REGION-DEMO-001')
const parentId = ref('REGION-BJ')
const bandwidth = ref(500)
const compareBandwidth = ref(1500)
const parentOptions = ref<RegionNode[]>([])
const board = ref<DensityBoard | null>(null)
const comparison = ref<BandwidthCompare | null>(null)
const approvals = ref<ApprovalSummary[]>([])
const approval = ref<ApprovalDetail | null>(null)
const replay = ref<ReplayResult | null>(null)
const reconcile = ref<ReconcileResult | null>(null)
const olderApprovalId = ref('')
const busy = ref(false)
const errorMessage = ref('')
const notice = ref('')
const loadingHint = ref('')

const ready = computed(() => board.value !== null)
const olderOptions = computed(() =>
  approvals.value.filter((item) => item.approval_id !== approval.value?.approval_id),
)

function fail(error: unknown) {
  errorMessage.value = error instanceof Error ? error.message : '操作失败'
}

function reset() {
  errorMessage.value = ''
  notice.value = ''
}

async function seedDemo() {
  busy.value = true
  reset()
  try {
    const result = await createDemoBatch()
    notice.value = `${result.message}（${result.points} 个定位点）`
    await reloadBoard()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function reloadBoard() {
  if (!batchKey.value.trim()) {
    errorMessage.value = '请先填写批次编号'
    return
  }
  busy.value = true
  reset()
  loadingHint.value = '读取密度看板…'
  try {
    board.value = await loadDensityBoard(batchKey.value.trim(), {
      parentId: parentId.value,
      bandwidth: bandwidth.value,
    })
    await refreshApprovals()
  } catch (error) {
    board.value = null
    fail(error)
  } finally {
    busy.value = false
    loadingHint.value = ''
  }
}

async function runCompare() {
  busy.value = true
  reset()
  loadingHint.value = '两种带宽试算…'
  try {
    comparison.value = await loadBandwidthCompare(
      batchKey.value.trim(),
      [bandwidth.value, compareBandwidth.value],
      parentId.value,
    )
    if (!comparison.value.same_sample_set) {
      errorMessage.value = '两轮定位点不一致，结果不可直接对照'
    }
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
    loadingHint.value = ''
  }
}

async function refreshApprovals() {
  const payload = await listApprovals(batchKey.value.trim())
  approvals.value = payload.items
}

async function openApproval() {
  busy.value = true
  reset()
  try {
    approval.value = await openApprovalApi({
      batch_key: batchKey.value.trim(),
      parent_id: parentId.value,
      bandwidth: bandwidth.value,
      label: `${bandwidth.value}m 区域合计对照`,
    })
    replay.value = null
    await refreshApprovals()
    notice.value = `已打开 ${approval.value.approval_id}，输入与网格已冻结`
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function selectApproval(approvalId: string) {
  reset()
  try {
    approval.value = await getApproval(approvalId)
    replay.value = null
    reconcile.value = null
    olderApprovalId.value = ''
  } catch (error) {
    fail(error)
  }
}

async function verifyDemoLatePoint() {
  if (!approval.value) return
  busy.value = true
  reset()
  try {
    const result = await addDemoLatePoint(approval.value.approval_id)
    notice.value = `${result.point_key} 已通过位置核验，归属 ${result.region_name}`
    approval.value = await getApproval(approval.value.approval_id)
    await refreshApprovals()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function confirmSelected(approvalId: string, decision = '历史对账') {
  busy.value = true
  reset()
  try {
    approval.value = await confirmApproval(approvalId, decision)
    await refreshApprovals()
    notice.value = `${approvalId} 已确认（${approval.value.decision}）`
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function confirmCorrected() {
  if (!approval.value) return
  await confirmSelected(approval.value.approval_id, '更正补采')
}

async function runReplay(useCurrent: boolean) {
  if (!approval.value) return
  reset()
  try {
    replay.value = await replayApproval(approval.value.approval_id, useCurrent)
    reconcile.value = null
  } catch (error) {
    fail(error)
  }
}

async function runReconcile() {
  if (!approval.value || !olderApprovalId.value) return
  reset()
  try {
    reconcile.value = await reconcileApprovals(approval.value.approval_id, olderApprovalId.value)
    replay.value = null
  } catch (error) {
    fail(error)
  }
}

onMounted(async () => {
  try {
    const hierarchy = await loadHierarchy()
    parentOptions.value = hierarchy.parent
      ? [hierarchy.parent, ...hierarchy.subregions]
      : hierarchy.subregions
    if (!parentOptions.value.length) parentOptions.value = [{ region_id: 'REGION-BJ' } as RegionNode]
  } catch (error) {
    fail(error)
  }
  await reloadBoard()
})
</script>

<style scoped>
.density-page { display: flex; flex-direction: column; gap: 16px; }
.board-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.board-column { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.board-column.class-observed { border-top: 3px solid #22c55e; }
.board-column.class-kernel { border-top: 3px solid #f59e0b; }
.board-column.class-zero { border-top: 3px solid #ef4444; }
.board-column header { display: flex; justify-content: space-between; align-items: baseline; }
.board-count { color: var(--muted); font-size: 12px; }
.board-desc { color: var(--muted); font-size: 12px; margin: 4px 0 8px; }
.board-cells { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 360px; overflow: auto; }
.board-cells li { border: 1px solid #eef1f5; border-radius: 6px; padding: 8px; font-size: 12px; }
.cell-head { display: flex; justify-content: space-between; }
.cell-region { color: var(--muted); }
.cell-meta, .cell-reason { margin: 4px 0 0; color: var(--muted); }
.cell-sources { margin: 4px 0 0; color: #475569; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-note { color: var(--muted); font-size: 12px; margin: 8px 0 0; }
.point-list span { display: inline-block; margin: 0 6px 4px 0; padding: 1px 6px; background: #f1f5f9; border-radius: 4px; font-size: 11px; }
.compare-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.compare-card { border: 1px solid #eef1f5; border-radius: 6px; padding: 10px; }
.compare-card h4 { margin: 0 0 6px; }
.data-table.compact th, .data-table.compact td { padding: 5px 8px; font-size: 12px; }
.boundary-list { list-style: none; margin: 6px 0 0; padding: 0; font-size: 12px; display: flex; flex-wrap: wrap; gap: 8px; }
.boundary-list li { background: #f8fafc; border: 1px solid #eef1f5; border-radius: 6px; padding: 4px 8px; display: flex; gap: 8px; }
.approval-detail { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 12px; }
.approval-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
.older-picker { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); max-width: 360px; margin: 8px 0; }
.older-picker select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; }
.replay-result, .backfill-plan { background: #f8fafc; border-radius: 6px; padding: 8px 12px; font-size: 12px; }
.notice-text { color: #0f766e; font-size: 13px; }
</style>
