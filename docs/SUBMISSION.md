# RegressLens · DOAI 作品提交素材

## 视频标题

RegressLens｜从推理性能回归到可验证的 Git 提交

## 视频简介

当同一个模型和工作负载在新版本变慢时，RegressLens 帮开发者用配对 Benchmark 验证退化、用 Git 二分定位可疑提交、用隔离回退测试根因假设。Dashboard 可以并排比较 Good / Bad Trace、差分火焰图、GPU kernel 指标和实验原始记录，终端 Agent 也可通过 19 个 CLI 入口及统一 JSON 驱动调查。

演示版本为 v0.2.1。受控案例检测到长输入负载的 TTFT p95 退化 31.0%，定位到调度准入变更。隔离回退后 TTFT p95 从 106.9 ms 降至 81.6 ms。项目通过 18 项测试，并验证了真实 PyTorch CPU Trace 转换。真实 vLLM GPU 采集待验收。

## 四分钟章节

| 时间 | 内容 |
|---|---|
| 00:00 | 从推理变慢到提交 |
| 00:18 | 配对试验与回归判定 |
| 00:41 | TTFT p95 退化和置信区间 |
| 00:59 | Git 二分定位 |
| 01:19 | 独立回退验证 |
| 01:41 | Good / Bad Trace |
| 02:05 | 差分火焰图 |
| 02:25 | GPU kernel 明细 |
| 02:40 | 实验记录、导出与复现 |
| 03:00 | Agent / CLI 调用 |
| 03:24 | 实测范围 |
| 03:46 | 总结 |

## 表单对应

| 表单字段 | 使用的素材 |
|---|---|
| Demo 视频链接（必填） | 上传 `RegressLens-DOAI-4min.mp4` 后，填写评委能打开的视频播放页链接。 |
| 在线 Demo / 官网（选填） | 使用已授权发布的作品页链接，作品页内有交互报告。 |
| GitHub 仓库链接（选填） | 发布 `RegressLens-v0.2.1.zip` 中的仓库后，填写评委可访问的仓库 URL。 |
| 在线项目简介文档（选填） | 可用这份文档的在线分享页，或项目介绍页。 |

## README 演示

将 `RegressLens-README.gif` 放在仓库的 `docs/media/`，然后插入

```markdown
![RegressLens demo](docs/media/RegressLens-README.gif)
```

## 制作来源

软件画面采自本地运行的 RegressLens v0.2.1。视频展示实录的浏览器交互、原始产品截屏及从真实 CLI 响应提取的摘要。中文旁白由本地 Piper 合成（zh_CN huayan medium），背景配乐为程序合成原创音轨。演示数值为标注的受控案例。
