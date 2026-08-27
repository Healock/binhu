import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('研判页区分已下发和未下发数据研判', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const analysisSource = readFileSync(
    new URL('../src/pages/AnalysisWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const mobileTaskSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const panelSource = readFileSync(
    new URL('../src/components/PoliceDispatchPanel.tsx', import.meta.url),
    'utf8',
  )
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')

  assert.match(pageSource, /mode = 'all'/)
  assert.match(pageSource, /analysisOnly \? 'pending_review'/)
  assert.match(pageSource, /analysisOnly \? 'manual'/)
  assert.match(pageSource, /研判结果仍保存在原下发批次/)
  assert.match(pageSource, /SearchOutlined[\s\S]*from '@ant-design\/icons'/)
  assert.match(analysisSource, /已下发数据研判/)
  assert.match(analysisSource, /未下发数据研判/)
  assert.match(analysisSource, /已下发数据中的研判事项/)
  assert.doesNotMatch(analysisSource, /网格核查研判|下发数据复核/)
  assert.match(pageSource, /analysisOnly \? '未下发数据研判'/)
  assert.match(pageSource, /尚未下发、无法直接确定去向的数据/)
  assert.match(analysisSource, /<MobileTaskList mode="analysis"[\s\S]*onAnalysisCountChange=/)
  assert.match(analysisSource, /mobile-task-priority-card/)
  assert.match(analysisSource, /flowCount/)
  assert.match(analysisSource, /dispatchCount/)
  assert.match(pageSource, /onAnalysisCountChange/)
  assert.match(pageSource, /police-dispatch-analysis-counts/)
  assert.match(mobileTaskSource, /mobile-task-analysis-stage-grid/)
  assert.match(mobileTaskSource, /ALL_ANALYSIS_TYPES/)
  assert.match(mobileTaskSource, /label: '全部数据'/)
  assert.match(mobileTaskSource, /listMobileTaskAnalysis/)
  assert.match(mobileTaskSource, /getMobileTaskAnalysisFilterOptions/)
  assert.match(mobileTaskSource, /analysisOnly \? 'waiting_analysis'/)
  assert.match(mobileTaskSource, /review_stage:\s*reviewStage/)
  assert.match(mobileTaskSource, /研判阶段筛选/)
  assert.match(mobileTaskSource, /waiting_analysis.*待研判/)
  assert.match(mobileTaskSource, /analyzed.*已研判/)
  assert.match(panelSource, /status=pending_review&category=manual/)
  assert.match(appSource, /path="\/police-analysis"/)
  assert.match(appSource, /<AnalysisWorkbench \/>/)
})

test('下发状态筛选并入批次统计并显示分组数量', () => {
  const source = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )

  assert.match(source, /const publishStatusOptions = \[/)
  assert.match(source, /police-dispatch-status-filter-grid/)
  assert.match(source, /police-dispatch-status-metric__count/)
  assert.match(source, /activeBatch\.counts\.pending_review/)
  assert.match(source, /activeBatch\.counts\.pending_publish/)
  assert.match(source, /activeBatch\.counts\.duplicate/)
  assert.match(source, /activeBatch\.counts\.abnormal/)
  assert.match(source, /completedCount/)
  assert.match(source, /activeBatch\.counts\.total/)
  assert.match(source, /label: '未发布'/)
  assert.match(source, /label: '可重试'/)
  assert.match(source, /label: '待对账'/)
  assert.match(source, /label: '内容冲突'/)
  assert.match(source, /activeBatch\.counts\[option\.value\]/)
  assert.match(source, /selectTaskFilter\('pending_review', option\.value\)/)
  assert.match(source, /const reconciliationHint = '腾讯写入结果不确定/)
  assert.match(styles, /\.police-dispatch-status-children--review/)
  assert.match(styles, /\.police-dispatch-status-children--publish/)
  assert.match(styles, /\.police-dispatch-status-chip\.is-active/)
})

test('使用搜索图标的下发和小区页面显式导入图标', () => {
  const dispatchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const batchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchBatchDetail.tsx', import.meta.url),
    'utf8',
  )
  const addressSource = readFileSync(
    new URL('../src/pages/PoliceAddressManagement.tsx', import.meta.url),
    'utf8',
  )

  for (const source of [dispatchSource, batchSource, addressSource]) {
    assert.match(source, /SearchOutlined[\s\S]*from '@ant-design\/icons'/)
    assert.match(source, /prefix=\{<SearchOutlined \/>\}/)
  }
})

test('已处理数据在任务工作台多选发布且卡片沿用流口任务规范', () => {
  const workbenchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const panelSource = readFileSync(
    new URL('../src/components/PoliceDispatchPanel.tsx', import.meta.url),
    'utf8',
  )
  const batchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchBatchDetail.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(workbenchSource, /选择发布/)
  assert.match(workbenchSource, /全选当前筛选/)
  assert.match(workbenchSource, /已选 \{selectedCount\} 条/)
  assert.match(workbenchSource, /发布所选/)
  assert.match(workbenchSource, /getPoliceDispatchPublishableSelection/)
  assert.match(workbenchSource, /publishSelectedPoliceDispatchTasks/)
  assert.match(workbenchSource, /resolvePoliceDispatchDuplicateGroup/)
  assert.match(workbenchSource, /保留此条/)
  assert.match(workbenchSource, /其余记录会同时标记为重复排除/)
  assert.match(workbenchSource, /pendingResolvedDuplicateId/)
  assert.match(clientSource, /resolve-duplicate/)
  assert.match(workbenchSource, /selectionMode \? 'is-selection-mode'/)
  assert.match(workbenchSource, /selected \? 'is-selected'/)
  assert.match(workbenchSource, /该条已经审核，无需逐条保存/)
  assert.match(workbenchSource, /'mobile-task-item-card police-dispatch-task-card'/)
  assert.match(workbenchSource, /mobile-task-item-card__key-row--identity/)
  assert.match(workbenchSource, /mobile-task-item-card__key-row--phone/)
  assert.match(workbenchSource, /mobile-task-item-card__key-row--address/)
  assert.match(workbenchSource, /mobile-task-analysis__label">平台建议/)
  assert.match(workbenchSource, /mobile-task-source-cloud--card/)
  assert.match(workbenchSource, /mobile-task-item-card__footer/)
  assert.doesNotMatch(workbenchSource, /<CopyButton\b/)
  assert.match(workbenchSource, /function CopyIconButton/)
  assert.match(workbenchSource, /<CopyIconButton value=\{value\} label=\{label\} \/>/)
  assert.match(panelSource, /status=pending_publish&category=all/)
  assert.match(panelSource, /确认导入到下发工作台/)
  assert.match(panelSource, /'rental_processed', 'police_suzhou_processed', 'police_traffic_processed'/)
  assert.doesNotMatch(panelSource, /确认导入并发布可发布项/)
  assert.doesNotMatch(panelSource, /整批发布/)
  assert.match(panelSource, /pending_review&category=manual/)
  assert.match(workbenchSource, /批次详情/)
  assert.match(workbenchSource, /\/police-dispatch\/batches\/\$\{activeBatch\.id\}/)
  assert.doesNotMatch(batchSource, /整批发布/)
  assert.doesNotMatch(batchSource, /publishSelectedPoliceDispatchTasks/)
})

test('业务数据导入支持多业务表快捷下发并仍由发布工作台落表', () => {
  const panelSource = readFileSync(
    new URL('../src/components/PoliceDispatchPanel.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(panelSource, /快捷下发/)
  assert.doesNotMatch(panelSource, /快捷下发（全链条）/)
  assert.match(panelSource, /quickBusinesses/)
  assert.match(panelSource, /selectedQuickBusiness\.fields\.map/)
  assert.match(panelSource, /profile: String\(values\.profile \|\| quickProfile\)/)
  assert.match(panelSource, /quickProfile/)
  assert.match(panelSource, /适用于 1–2 条临时任务/)
  assert.match(panelSource, /提交后只加入下发任务池，不会立即写入腾讯表/)
  assert.match(panelSource, /createQuickPoliceDispatch/)
  assert.match(panelSource, /getQuickDispatchOptions/)
  assert.match(panelSource, /status=pending_publish&category=all/)
  assert.match(clientSource, /api\.post\('\/police-dispatch\/quick-dispatch'/)
  assert.match(clientSource, /api\.get\('\/police-dispatch\/quick-dispatch\/options'/)
  assert.match(clientSource, /import_mode: 'raw' \| 'clean' \| 'processed' \| 'quick'/)
})

test('选中发布进入可离页恢复的后台任务并持续展示安全分类进度', () => {
  const workbenchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(workbenchSource, /getLatestPoliceDispatchPublishRun/)
  assert.match(workbenchSource, /getPoliceDispatchPublishRun/)
  assert.match(workbenchSource, /window\.setInterval[\s\S]*2000/)
  assert.match(workbenchSource, /可以离开本页面，服务器会继续处理/)
  assert.match(workbenchSource, /成功 <strong>\{publishRun\.success_count\}/)
  assert.match(workbenchSource, /冲突 <strong>\{publishRun\.conflict_count\}/)
  assert.match(workbenchSource, /待对账 <strong>\{publishRun\.reconciliation_count\}/)
  assert.match(workbenchSource, /可重试 <strong>\{publishRun\.retryable_count\}/)
  assert.match(clientSource, /api\.post\(`\/police-dispatch\/batches\/\$\{id\}\/publish-selected`/)
  assert.match(clientSource, /publish-runs\/latest/)
  assert.match(clientSource, /publish-runs\/\$\{runId\}/)
})
