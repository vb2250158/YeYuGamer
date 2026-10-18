# 人工截图审核

`agent-launch` 使用本机 bearer 凭据，除了原执行启动权限，具有精确端点的运行读取、运行控制、事项复核、证据读写权限。它可读取 Todo、GameRun 及其尝试、artifact；可请求取消、恢复、接管和释放接管，以及记录人工证据审核。不得读写配置、管理凭据、操作宿主生命周期或接管 Agent 工作项。scheduler 保持原权限。

事项状态转换只允许该角色提交 `review_required`，不能直接完成事项、增加执行次数或释放技术阻塞。所有写入仍要求当前 `If-Match` 和 `Idempotency-Key`，身份由凭据绑定，不相信正文的 `requestedBy`。

## 上传并关联待审核事项

`POST /api/v1/todo-instances/{todo_id}/evidence-uploads`

JSON 字段：`fileName`（PNG/JPEG 文件名，不含路径）、`contentBase64`（严格 base64）、`note`（来源和待核实说明）。仅此端点允许最多 12 MiB 请求体，图片原文件最多 8 MiB、2400 万像素；Manager 验证实际图片格式和可解码性，不读取调用者提供的任意本机路径。

Manager 原子记录 artifact 与当前周期事项的 `review_required` 状态，返回 command receipt，`result.evidenceId` 是生成的证据 ID，`result.artifact` 可供预览。重复相同幂等键和内容不产生第二张图；不同内容冲突、过期 CAS、坏图和非法状态均拒绝。执行中和已完成事项不接受该上传操作。已有证据和正式运行尝试保留；不增加尝试次数。

Python 客户端提供 `ManagerApiClient.upload_todo_screenshot(...)`，读取有界本机文件后上传。WebGUI 的事项详情内提供“人工截图与审核”，可选择图片、填写来源说明、上传、预览，并进入证据页记录接受/拒绝。

上传图片标记 `source=operator-upload`、`diagnosticOnly=true`；时间只表示收到上传，实际截图时间、账号身份和 RunAttempt 不由上传声明证明。不伪造官方识别输入、RunAttempt 或程序领奖校验。接受单个 artifact 仅记录人工核实，不会把 Todo 或游戏标为 `accepted_done`。
