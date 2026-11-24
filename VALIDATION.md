# 赛事 MVP 验收记录

核验日期 2026-11-24。

| 阶段 | 可复现核验 |
|---|---|
| Phase 1 | GuideLLM 0.8.1 实际 CLI 进入请求调度，配置通过该版本的 Pydantic schema。已修正 PyTorch DataLoader 的 prefetch 参数。当前执行环境在创建进程间 socket 时拒绝操作，完整 JSON 产出待 Colab 复验。 |
| Phase 2 | 受控 Git 案例分别覆盖 FAIL、PASS、INCONCLUSIVE、INVALID。配对统计测试覆盖吞吐方向、缺失样本、重复配对和配置漂移。 |
| Phase 3 | 九个本地提交通过真实 Git worktree 二分，跳过夹具中的构建失败，并定位到植入的调度变更。测试覆盖预算中断后复用已完成轮次。 |
| Phase 4 | Scheduler、KV Cache、Decode、Prefill 和环境偏差规则均已实现。调度夹具记录了 Bad 与隔离回退、Good 与隔离回退两个独立对照。 |
| Phase 5 | React / TypeScript / ECharts 构建完成，四视图、FastAPI、JSON / HTML / Markdown 导出、演示与 Colab Notebook 已打包。 |

## 尚待真实 GPU 验证

Colab 已创建 Notebook，选择了 T4 并启动预检。浏览器会话中断，未取得 GPU 分配结果。附带的 Notebook 使用同一模型和 vLLM 版本，对真实引擎注入单项调度限制，用于硬件闭环验证。真实 upstream 源码回归需要另选实际 Good / Bad SHA 和构建包装脚本。当前示例报告均标为受控夹具。

本地也尝试过 vLLM 0.13.0 CPU 服务和进程内推理。其 ZeroMQ IPC / PyTorch Gloo 初始化在当前执行环境被拒绝，未产生可报告的推理性能结果。

## 可复现结果

| 观察 | 数值 |
|---|---|
| 回归夹具 TTFT p95 | +31.0%，95% 区间 +30.0% 到 +32.0% |
| 定位 | `465125feec57`，调度准入变更 |
| Bad → 隔离回退 | −23.7%，95% 区间 −24.2% 到 −23.1% |
| 高噪声夹具 | +5.6%，95% 区间 −16.8% 到 +28.0%，INCONCLUSIVE |

测试结果为 11 项通过。前端 TypeScript 构建通过。在浏览器中核验了四个视图、指标切换、提交过滤、证据跳转、Markdown / JSON 下载和 390px 窄屏布局。配套短片从同一份夹具 JSON 生成。

## v0.2.0 Profiling

- 导入测试覆盖 Chrome Trace JSON / gzip、X 与 B/E 事件、Speedscope 采样和事件栈、GPU 累计耗时，以及导入失败保留旧记录。
- 用 PyTorch 2.8.0 CPU Profiler 分别生成两份真实 Trace，两份都转换了 Python span 栈。修正了绝对时间戳浮点舍入对嵌套关系的影响。
- 通过受控测试验证了 Good / Bad 重放、Profiler 启停以及 Benchmark gate 与 Profiling 的隔离。
- 在浏览器验证了时间轴缩放和筛选、差分火焰图、GPU 明细、390 px 布局、原始文件下载和 Perfetto 消息握手。握手以本地模拟的 Viewer 核验。
- 真实 GPU 上的 vLLM 重放采集仍待验收。附带的离线报告使用受控样例。

## v0.2.1 CLI automation

18 项测试通过。新增端到端用例通过独立进程调用 CLI，完成调查创建、恢复运行、二分查询、回归比对、登记验证、回退实验、Profiling 导入、原始文件导出和 HTML 导出。用例检查 JSON 合约、执行退出码、路径越界和重复导出的行为。真实 GPU 采集的验收状态沿用 v0.2.0。
