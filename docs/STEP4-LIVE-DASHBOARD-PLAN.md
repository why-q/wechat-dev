# Step 4：实时 Dashboard 图表接入、工单结案与 7 天留存清理计划

> 日期：2026-11-28  
> 范围：P0（Demo 布局与图表接入 `/dashboard/`、馆员工单结案与反馈展示、CSS 交付优化）+ P1（`inbound_events` 7 天到期清理；紧急告警暂缓）。

## 1. 现状与目标

- 静态演示页 `docs/dashboard-demo/index.html` 与 `librarian.html` 已在 `/demo/` 上线（1040px 紧凑栅格、内联 SVG 图表、内联 CSS 零阻塞首屏）。
- 线上 `/dashboard/admin` 与 `/dashboard/librarian` 仍为纯表格与只读私信流，缺少时间序列统计、分流路径图、工单结案闭环与反馈列表。
- 已确认：微信云托管 `WECHAT_FORWARD_URL` 已切换至 `wechat-dev.997881.xyz`；本地 `master` 已推送至 `origin/main`。
- 回复边界不变：馆员仍在微信公众号后台回复读者，Dashboard 仅负责待办查看、备注登记与结案（不直连 `custom/send`）。

## 2. 并行拆解与接口契约（git worktree）

### Track A：后端统计聚合 + 7 天过期清理
- 文件：`support_backend/db.py`、`support_backend/pipeline.py`、`relay_receiver.py`、`tests/test_support_backend.py`
- `SupportStore.purge_expired_events(now_epoch=None) -> int`：
  - 筛选 `expires_at_epoch IS NOT NULL AND expires_at_epoch <= now_val`，排除仍在 `open`/`processing`/`queued` 批次中的事件。
  - 在同一事务内：删除对应 `batch_messages`、将 `staff_case_messages.event_seq` 置 `NULL`、删除过期 `inbound_events`，满足 `PRAGMA foreign_keys=ON`。
- `SupportPipeline`：在 `start()` 启动时及 `_scheduler_loop()` 中按 `purge_interval_seconds`（默认 3600s）周期执行 `purge_expired_events()`。
- `relay_receiver.py`：新增 Bearer 鉴权接口 `POST /relay/support/purge-expired`。
- `SupportStore.dashboard_admin_analytics(now_epoch=None) -> dict`（全部按 UTC+8 统计）：
  - `snapshot_at_utc8`, `date_range_label`
  - `daily_inbound`: 7 天数组 `[{"date": "YYYY-MM-DD", "label": "28六", "count": int}]`
  - `inbound_7d_total`, `inbound_7d_avg`, `inbound_today_total`, `inbound_all_total`
  - `hourly_inbound_today`: 当日分时数组 `[{"hour": int, "label": "08", "count": int}]`（覆盖 `08..max(16, cur_hour)` 并包含所有非零小时）
  - `replies_today` / `replies_all`: `{"sent": int, "shadow": int, "failed": int, "total": int}`
  - `filter_7d`: `{"question", "feedback", "invalid", "unsupported", "emergency", "gray_enroll", "total_non_gray"}`
  - `routing_7d`: `{"tier2_rag", "tier1_preset", "clarify", "staff_fallback", "other", "total", "auto_rate_pct"}`
  - `batches`: `{"open", "processing", "queued", "flushed", "active_total"}`
  - `open_cases`: `{"total", "emergency", "staff_todo"}`
  - `recent_alerts`: 最近 5 条脱敏异常（发信失败、紧急工单、转人工兜底；不含原文与 OpenID）
- `SupportStore.dashboard_librarian_analytics(now_epoch=None) -> dict`（UTC+8，**严禁返回 `reader_id`**）：
  - `snapshot_at_utc8`
  - `open_emergency` / `open_todo`: 未结 `staff_cases` 列表（按等待时长降序），每条含 `case_id`, `case_kind`, `summary`, `latest_message`, `created_at_utc8`, `created_hm`, `wait_minutes`, `messages`（只含 `sender_role`, `content`, `created_hm`）
  - `open_counts`: `{"total", "emergency", "staff_todo"}`
  - `wait_buckets`: `{"emergency", "over_60m", "m30_60", "under_30m"}`
  - `longest_wait`: `{"minutes": int, "created_hm": str, "summary": str}`
  - `today_stats`: `{"created", "closed", "close_rate_pct"}`
  - `response_stats`: `{"avg_minutes": float | None, "recent_durations": [int], "sample_count": int, "scope": "today" | "recent"}`
  - `latency_buckets`: `{"le_10m", "m10_20", "m20_30", "gt_30m", "total", "within_30m_pct"}`
  - `hourly_pace_today`: `[{"hour": int, "label": "09:00", "created": int, "closed": int}]`
  - `recent_closed`: 最近 15 条已结工单（不含 `reader_id`，含 `case_id`, `case_kind`, `summary`, `closed_at_utc8`, `resolution_note`, `duration_minutes`）
  - `recent_feedbacks`: 最近 20 条意见反馈（不含 `reader_id`，含 `feedback_id`, `created_at_utc8`, `content`, `status`, `staff_note`）

### Track B：内联 SVG 图表与 KPI 渲染器
- 文件：`support_backend/dashboard_charts.py`、`tests/test_dashboard_charts.py`
- 纯函数实现，复用 `docs/dashboard-demo/dashboard.css` 类名，严格转义 HTML/SVG 文本、零脚本、零除保护（全 0 数据时优雅显示空态）。

### Track C：馆员工作台组件（工单队列、结案表单、反馈与私信）
- 文件：`support_backend/dashboard_librarian.py`、`tests/test_dashboard_librarian.py`
- 保持 `support_backend/dashboard_inbox.py` 现有 `render_inbox` 签名与输出兼容。
- 结案表单：`POST /dashboard/librarian/cases/{case_id}/close`，同源 CSRF + Basic Auth（`admin` / `librarian` 均可），调用 `store.close_staff_case(case_id, resolution_note=...)`。

### 汇合与上线
- 合并三个 worktree 分支，在 `support_backend/dashboard.py` 接入新视图与内联压缩 CSS（同步修复 systemd `BindReadOnlyPaths` 目标路径避免在 `support_backend/` 生成 0 字节文件）。
- 运行完整单元测试，重启 `wechat-relay.service`，校验公网 HTTPS 接口，写入 `PROGRESS.jsonl` 与 `BACKGROUND.md` 并推送。
