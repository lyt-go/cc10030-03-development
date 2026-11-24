/** 区域密度相关接口：只有区域维度和密度估算共用，挂在固定路径下。 */

export type Bounds = { south: number; west: number; north: number; east: number }

export type RegionNode = {
  region_id: string
  name: string
  level: string
  parent_id: string
  parent_name: string
  boundary_code: string
  status: string
  bounds: Bounds
  has_bounds: boolean
}

export type SubregionDensity = {
  region_id: string
  region_name: string
  bounds: Bounds
  sample_count: number
  sample_points: string[]
  grid_count: number
  grids: string[]
  observed_cells: number
  kernel_cells: number
  zero_cells: number
  kernel_sum: number
  weight_total: number
  bandwidth: number
  kernel: string
  grid_level: string
  backfill_gaps: { grid_geohash: string; nearest_distance_m: number; reason: string }[]
}

export type CellSource = { point_key: string; weight: number; distance_m: number }

export type BoardCell = {
  grid_geohash: string
  center: [number, number]
  owner_region_id: string
  owner_region_name: string
  sample_count: number
  kernel_sum: number
  nearest_distance_m: number
  reason: string
  sources: CellSource[]
}

export type DensityBoard = {
  batch_key: string
  parent: RegionNode
  inputs: {
    bandwidth: number
    kernel: string
    grid_level: string
    sample_count: number
    sample_fingerprint: string
  }
  columns: { key: 'observed' | 'kernel' | 'zero'; label: string; count: number; description: string }[]
  cells: Record<'observed' | 'kernel' | 'zero', BoardCell[]>
  subregions: SubregionDensity[]
  totals: {
    sample_count: number
    subregion_sample_count: number
    grid_count: number
    observed_cells: number
    kernel_cells: number
    zero_cells: number
    kernel_sum: number
  }
  reconciliation: Reconciliation
}

export type Reconciliation = {
  parent_sample_count: number
  subregion_sample_total: number
  unassigned_points: string[]
  point_sets_match: boolean
  cross_boundary_grids: {
    grid_geohash: string
    owners: { region_id: string; overlap_ratio: number }[]
  }[]
  note: string
}

export type BandwidthRound = {
  bandwidth: number
  sample_count: number
  sample_fingerprint: string
  observed_cells: number
  kernel_cells: number
  zero_cells: number
  kernel_sum: number
  subregions: {
    region_id: string
    region_name: string
    sample_count: number
    sample_points: string[]
    grid_count: number
    observed_cells: number
    kernel_cells: number
    zero_cells: number
    kernel_sum: number
  }[]
  reconciliation: Reconciliation
}

export type BandwidthCompare = {
  batch_key: string
  parent: RegionNode
  kernel: string
  rounds: BandwidthRound[]
  same_sample_set: boolean
  note: string
}

export type ApprovalSummary = {
  approval_id: string
  label: string
  batch_key: string
  parent_name: string
  bandwidth: number
  kernel: string
  grid_level: string
  status: string
  decision: string
  opened_at: number | null
  confirmed_at: number | null
  grid_count: number
  late_points: number
}

export type LatePoint = {
  point_key: string
  lat: number
  lon: number
  timestamp: number | null
  arrived_at: number
  verification: string
  region_id: string
  region_name: string
  note: string
}

export type ApprovalDetail = {
  approval_id: string
  label: string
  batch_key: string
  parent_id: string
  parent_name: string
  bandwidth: number
  kernel: string
  grid_level: string
  status: string
  decision: string
  opened_at: number
  confirmed_at: number | null
  confirmed_by: string
  grid_ids: string[]
  input_summary: {
    sample_count: number | null
    sample_fingerprint: string | null
    source: string | null
    verified: boolean | null
  }
  result: {
    sample_count: number
    sample_fingerprint: string
    subregions: {
      region_id: string
      region_name: string
      sample_count: number
      sample_points: string[]
      grids: string[]
      observed_cells: number
      kernel_cells: number
      zero_cells: number
      kernel_sum: number
      weight_total: number
    }[]
    totals: Record<string, number>
    reconciliation: Reconciliation
    cells: {
      grid_geohash: string
      owner_region_id: string
      cell_class: string
      sample_count: number
      kernel_sum: number
      source_point_keys: string[]
    }[]
    late_point_handling?: {
      decision: string
      verified_points: string[]
      pending_points: string[]
      note: string
    }
  }
  late_points: LatePoint[]
  backfill_plan: {
    backfill_id: string
    point_key: string
    region_id: string
    region_name: string
    status: string
    reason: string
  }[]
  history: { at: number; event: string; detail: string }[]
}

export type ReplayResult = {
  approval_id: string
  approval_status: string
  decision: string
  source: string
  grid_ids: string[]
  bandwidth: number
  kernel: string
  sample_count: number
  matches: boolean
  differences: { region_id: string; field?: string; stored?: number; replayed?: number; diff?: string }[]
  replayed: { region_id: string; region_name: string; sample_count: number; kernel_sum: number }[]
  stored: { region_id: string; region_name: string; sample_count: number; kernel_sum: number }[]
  note: string
}

export type DemoBatchResult = {
  ok: boolean
  batch_key: string
  points: number
  include_late: boolean
  message: string
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' } })
  if (!response.ok) throw new Error(await readError(response))
  return (await response.json()) as T
}

async function postJson<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(await readError(response))
  return (await response.json()) as T
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = await response.json()
    if (typeof payload.detail === 'string') return payload.detail
    if (payload.detail && typeof payload.detail === 'object') {
      const detail = payload.detail as { message?: string; errors?: string[] }
      return [detail.message, ...(detail.errors ?? [])].filter(Boolean).join('：')
    }
  } catch {
    /* 非 JSON 错误体，退回状态码说明 */
  }
  return `接口返回 ${response.status}，数据未更新`
}

export function loadHierarchy(parentId = 'REGION-BJ') {
  return getJson<{ parent: RegionNode | null; subregions: RegionNode[] }>(
    `/api/region_dim/hierarchy?parent_id=${encodeURIComponent(parentId)}`,
  )
}

export function loadDensityBoard(
  batchKey: string,
  options: { parentId?: string; bandwidth?: number; kernel?: string } = {},
) {
  const params = new URLSearchParams({
    batch_key: batchKey,
    parent_id: options.parentId ?? 'REGION-BJ',
    bandwidth: String(options.bandwidth ?? 500),
    kernel: options.kernel ?? 'gaussian',
  })
  return getJson<DensityBoard>(`/api/region_dim/density-board?${params.toString()}`)
}

export function loadBandwidthCompare(
  batchKey: string,
  bandwidths: [number, number],
  parentId = 'REGION-BJ',
) {
  const params = new URLSearchParams({
    batch_key: batchKey,
    bandwidths: bandwidths.join(','),
    parent_id: parentId,
  })
  return getJson<BandwidthCompare>(`/api/region_dim/bandwidth-compare?${params.toString()}`)
}

export function listApprovals(batchKey?: string) {
  const suffix = batchKey ? `?batch_key=${encodeURIComponent(batchKey)}` : ''
  return getJson<{ items: ApprovalSummary[]; total: number }>(`/api/region_dim/approvals${suffix}`)
}

export function getApproval(approvalId: string) {
  return getJson<ApprovalDetail>(`/api/region_dim/approvals/${encodeURIComponent(approvalId)}`)
}

export function openApproval(values: {
  batch_key: string
  parent_id?: string
  bandwidth?: number
  label?: string
}) {
  return postJson<ApprovalDetail>('/api/region_dim/approvals', values)
}

export function addLatePoint(approvalId: string, point: Record<string, unknown>, regionId?: string) {
  return postJson<{ ok: boolean; point_key: string; region_name: string; note: string }>(
    `/api/region_dim/approvals/${encodeURIComponent(approvalId)}/late-points`,
    { point, region_id: regionId },
  )
}

export function addDemoLatePoint(approvalId: string) {
  return postJson<{ ok: boolean; point_key: string; region_name: string; note: string }>(
    `/api/region_dim/demo-late-point?approval_id=${encodeURIComponent(approvalId)}`,
  )
}

export function confirmApproval(approvalId: string, decision: string) {
  return postJson<ApprovalDetail>(
    `/api/region_dim/approvals/${encodeURIComponent(approvalId)}/confirm`,
    { decision, operator: '值班员' },
  )
}

export function replayApproval(approvalId: string, useCurrent = false) {
  return getJson<ReplayResult>(
    `/api/region_dim/approvals/${encodeURIComponent(approvalId)}/replay?use_current_inputs=${useCurrent}`,
  )
}

export function createDemoBatch(includeLate = false) {
  return postJson<DemoBatchResult>('/api/region_dim/demo-batch', {
    batch_key: 'REGION-DEMO-001',
    include_late: includeLate,
  })
}

export type ReconcileResult = {
  batch_key: string
  older: {
    approval_id: string
    status: string
    sample_count: number
    subregions: { region_id: string; region_name: string; sample_count: number; kernel_sum: number }[]
  }
  current: {
    approval_id: string
    status: string
    sample_count: number
    subregions: { region_id: string; region_name: string; sample_count: number; kernel_sum: number }[]
  }
  older_replay: { matches: boolean; source: string; sample_count: number }
  current_replay: { matches: boolean; source: string; sample_count: number }
  history_reproducible: boolean
  input_changes: { added_points: string[]; removed_points: string[]; same_sample_set: boolean }
  note: string
}

export function reconcileApprovals(approvalId: string, againstId: string) {
  return getJson<ReconcileResult>(
    `/api/region_dim/approvals/${encodeURIComponent(approvalId)}/reconcile?against_id=${encodeURIComponent(againstId)}`,
  )
}

export function loadingHint(action: string) {
  return `正在${action}…`
}
