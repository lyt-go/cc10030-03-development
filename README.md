# 地理空间轨迹与网格聚合分析平台

面向定位点采集、坐标标准化、网格切分、轨迹分段驻留识别、空间关联聚合与区域维度产出的地理空间数据分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/RegionDensityView.vue  区域密度贡献专题页
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/region_density.py  区域密度看板、带宽对照与对照审批接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/geo_density.py  网格几何、核贡献与子区归属的共享纯函数
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 定位源接入 | `geo_source` | 采集批次 | 批次编号、采集终端、定位方式 |
| 坐标标准化 | `coord_transform` | 转换规则 | 规则编号、源坐标系、目标坐标系 |
| 轨迹切分 | `track_segment` | 轨迹分段 | 分段编号、终端编号、起始时间 |
| 驻留识别 | `dwell_detect` | 驻留事件 | 事件编号、分段编号、中心坐标 |
| 网格索引 | `grid_index` | 网格单元 | 网格编号、网格层级、边界范围 |
| 围栏判定 | `geofence_rule` | 电子围栏 | 围栏编号、围栏形状、顶点序列 |
| 空间关联 | `spatial_join` | 关联任务 | 任务编号、左数据集、右数据集 |
| 兴趣点匹配 | `poi_match` | 匹配候选 | 候选编号、驻留事件、兴趣点编号 |
| 区域维度 | `region_dim` | 区域层级 | 区域编号、上级区域、区域名称 |
| 热力聚合 | `heatmap_aggregate` | 聚合窗口 | 窗口编号、网格层级、时间粒度 |
| 起止流量 | `od_matrix` | 流量单元 | 单元编号、起点网格、终点网格 |
| 路径还原 | `route_restore` | 还原路段 | 路段编号、分段编号、路段序列 |
| 密度估算 | `density_estimate` | 密度网格 | 估算编号、网格编号、带宽参数 |
| 轨迹质量 | `geo_quality` | 质量报告 | 报告编号、批次编号、漂移点数 |
| 点位归并 | `dedup_merge` | 归并批次 | 归并编号、批次编号、去重键列 |
| 分析快照 | `geo_snapshot` | 版本快照 | 快照编号、分析口径、网格层级 |
| 历史回补 | `geo_backfill` | 回补任务 | 回补编号、目标数据集、起始日期 |
| 结果导出 | `geo_export` | 导出任务 | 导出编号、快照编号、导出格式 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 区域密度与审批走固定接口 `/api/region_dim/*`，不占用通用资源路由。

## 区域密度贡献

区域维度除维护层级外，还提供按上级区域汇总的密度贡献视图：

- 网格按 geohash 几何中心计算核贡献，逐格分为**有落点**、**仅有核贡献**
  （相邻格扩散）与**确为零**（所有来源点都在带宽外，属于待补采缺口）。
- 子区归属按网格与边界的**交叠面积唯一确定**，跨区网格整格只计入一个子区，
  父区域样本合计始终回对同一批定位点；每块子区列出采用点、网格、带宽与样本量。
- 每次**对照审批**都会把实际采用的网格集合、输入快照和逐格结果写入
  `data/region_approvals/`；确认后仍可按当天快照复算，`use_current_inputs=true`
  才改用当前输入并显式列出差异。
- 迟到定位点必须经 `POST /api/region_dim/approvals/{id}/late-points`
  **独立通过位置核验**才会进入已核验输入；审批打开后不会被自动改写。
  确认时可选择「历史对账」（按旧观测完成核对）或「更正补采」
  （只采用已核验的更正点，并重新登记补采任务）。
- 区域估算只接受坐标标准化后的已核验定位点；只有原始采集点的批次会被拒绝。

常用接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/region_dim/hierarchy` | 父区域与子区边界 |
| `GET /api/region_dim/density-board` | 三类格分列、各子区采用点 |
| `GET /api/region_dim/density-view` | 按上级区域汇总的密度贡献 |
| `GET /api/region_dim/bandwidth-compare` | 两种带宽对照（同一批点） |
| `POST /api/region_dim/approvals` | 打开对照审批（冻结网格与结果） |
| `GET /api/region_dim/approvals/{id}/replay` | 按审批快照复算旧合计 |
| `GET /api/region_dim/approvals/{id}/reconcile` | 与更早审批做历史对账 |
| `POST /api/region_dim/demo-batch` | 生成横跨两个子区的演示批次 |

前端专题页：`http://127.0.0.1:5173/region_density`，也可从「区域维度」「密度估算」
模块页右上角的「区域密度贡献」进入。

领域：数据工程
子方向：数据分析与数据应用
