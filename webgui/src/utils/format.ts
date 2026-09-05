export function formatTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatJson(value: unknown): string {
  return JSON.stringify(value ?? null, null, 2)
}

export function humanize(value?: string | number | null): string {
  if (value === undefined || value === null || value === '') return '—'
  const labels: Record<string, string> = {
    accepted_done: '已验收完成',
    rejected_done: '完成证据被拒绝',
    evidence_pending: '等待证据',
    in_progress: '进行中',
    pending: '待处理',
    completed: '已勾选',
    skipped: '已跳过',
    blocked: '已阻塞',
    review_required: '待复核',
    not_started: '未开始',
    not_applicable: '不适用',
    unknown: '未知',
    agent_queued: '等待复核',
    agent_in_progress: '复核中',
    human_required: '需要人工',
    approval_required: '需要批准',
    resolved: '已解决',
    blocked_technical: '技术阻塞',
    human_takeover: '人工接管',
    retry_wait: '等待重试',
    terminal: '运行终态',
    planned: '已计划',
    pending_execution: '等待执行',
    queued: '排队中',
    cancelling: '正在安全停止',
    done: '运行结束',
    partial: '部分执行结束',
    preflight: '正在检查',
    launching: '正在启动',
    attaching: '正在连接游戏',
    observing: '正在确认画面',
    running: '运行中',
    verifying: '正在验证',
    executing: '正在执行',
    reconciling: '正在确认结果',
    cancelled: '已安全停止',
    crashed: '意外中断',
    accepted: '已受理',
    draft: '草稿',
    sending: '投递中',
    sent: '已送达',
    succeeded: '已成功',
    failed: '失败',
    rejected: '已拒绝',
    connected: '实时连接',
    reconnecting: '正在重连',
    connecting: '正在连接',
    offline: 'Manager 离线',
    mock: '开发 Mock',
    none: '无',
    healthy: '健康',
    automatic: '自动投递',
    manual_review: '人工复核',
    disabled: '已停用',
    configured: '已配置',
    secret_missing: '缺少凭据',
    invalid: '配置无效',
    transient_failure: '暂时失败',
    permanent_failure: '永久失败',
    ambiguous: '结果不确定',
    ready: '可执行',
    passed: '诊断通过',
    missing: '入口缺失',
    installed: '已安装',
    compatibility: '兼容阶段',
    'missing-entrypoint': '执行入口缺失',
    'unsafe-entrypoint': '执行入口不安全',
    'host-healthy-execution-package-missing': 'Host 健康 / 执行包缺失',
    'host-healthy-execution-package-unsafe': 'Host 健康 / 执行包不安全',
    'host-healthy-execution-disabled': 'Host 健康 / 执行未就绪',
  }
  return labels[String(value)] ?? String(value).replaceAll('_', ' ')
}

export function statusTone(value?: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  const text = value?.toLowerCase() ?? ''
  if (/(unsafe|unhealthy|invalid|ambiguous|permanent_failure)/.test(text)) return 'danger'
  if (/(missing|execution-disabled)/.test(text)) return 'warning'
  if (/(^ok$|^enabled$|^completed$|^sent$|^configured$|^automatic$|accepted_done|succeeded|healthy|resolved|connected|production)/.test(text)) return 'success'
  if (/(human|critical|failed|rejected|crashed|forbidden|offline)/.test(text)) return 'danger'
  if (/(pending|retry|warning|degraded|approval|blocked|candidate|shadow|manual_review|disabled|transient_failure)/.test(text)) return 'warning'
  if (/(running|progress|executing|verifying|queued|connected|mock|sending)/.test(text)) return 'info'
  return 'neutral'
}
