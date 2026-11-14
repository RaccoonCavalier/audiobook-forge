# 架构说明

## 1. 数据流

```
               ┌──────────────┐
  epub/txt/md ─▶│   ingest     │─▶ Book{chapters:[{blocks:[{text}]}]}
               └──────────────┘
                       │
                       ▼
               ┌──────────────┐
               │  llm/analyzer│─▶ Book{blocks:[{type,speaker,style,voice}]}
               │  选角 + 标注  │    Cast{narrator_voice, assignments[]}
               └──────────────┘
                       │
                       ▼
               ┌──────────────┐
               │   planner    │─▶ Segment[]（每段 ≈ 12~120 字，含 voice/style）
               └──────────────┘         │
                                        ▼  落库 SQLite，状态 pending
               ┌──────────────┐
               │  synthesize  │─▶ audio/00001.mp3 …（并发、重试、缓存）
               └──────────────┘         │
                                        ▼  状态 done
               ┌──────────────┐
               │   assemble   │─▶ out/书名.mp3 + 书名.m4b + manifest.json
               └──────────────┘
```

## 2. 为什么按"段"而不是按"章"合成

| 维度 | 按章合成 | 按段合成（本项目） |
|---|---|---|
| 失败重试 | 一章失败要重跑整章（可能 20 分钟） | 只重跑那 100 字 |
| 并发 | 一章一个请求，并发度为章节数 | 一个请求一段，可以开 4~8 路 |
| 音色切换 | 做不到 | 天然支持旁白/对白换音色 |
| 长度限制 | 容易超接口上限（讯飞 64K 字节） | 每段远低于上限 |
| 断点续跑 | 粒度粗 | 粒度细到每一句 |

代价是请求数变多（一本 10 万字的书约 1000~1500 段），固定开销上升。实践上用 12~120 字一段、并发 4 路，仍然是几十毫秒级的调度开销，不构成瓶颈。

## 3. 断点续跑

三段式状态机，全部落 SQLite：

```
pending ──▶ running ──▶ done
   ▲            │
   └────────────┴──────▶ failed ──(手动/自动重跑)──▶ pending
```

- 进程启动时 `reset_running()`：把所有卡在 `running` 的段放回 `pending`，避免"进程被杀导致永远不重试"。
- `abf synthesize` 只挑 `pending/failed/running` 的段。
- 每段合成成功立刻写库 + 落盘，不等全部结束。
- 分片文件存在但库里没记的，也会被识别并跳过（`Synthesizer._one` 里的第二道检查）。

## 4. 缓存

`cache/{sha256(provider|voice|style|text)[:32]}.mp3`

- 同一段文本 + 同一音色 + 同一风格 → 直接复用，不再请求 API。
- 重跑整本书时，只有文本真的变了的段才会重新花钱。
- 缓存不随书籍删除而清理（跨书共用），需要时手动删 `data/cache/`。

## 5. 失败处理

| 情况 | 行为 |
|---|---|
| 网络 5xx / 429 / 超时 | 指数退避重试（`ABF_TTS_RETRIES`，默认 3 次） |
| 鉴权失败 / 参数错误 / 未开通权限 | `retryable=False`，立即失败，不浪费时间 |
| 单段彻底失败 | 记录 `error`，**不影响其它段**，最后汇总报出前 10 条 |
| LLM 某批返回格式坏了 | 该批退回规则判断，其余批正常 |
| ffmpeg 失败 | 抛错并保留分片，可单独重跑 `assemble` |

## 6. 扩展一个新的 TTS 提供商

只需要三步：

1. 在 `tts/` 下新建类，继承 `BaseTTS`，实现 `async synthesize(request) -> TTSResult`。
2. 在 `tts/registry.py` 的 `create_tts()` 里加一个分支。
3. 在 `config.py` 加对应配置项，`.env.example` 补上说明。

`TTSResult.audio` 可以是 mp3/wav/pcm 任意格式——后面的 `transcode_bytes()` 会统一转成 44.1kHz 单声道 mp3，拼接层不关心来源格式。

## 7. 音色分配

`llm/analyzer.py` 里的 `VOICE_POOL` 是"provider × 性别 → 候选音色"的表。

- 有 LLM 时：LLM 给出角色和性别，按性别池轮转分配，保证不同角色不撞音色。
- 无 LLM 时：角色性别一律 `unknown`，混用男女声池轮转，至少听起来有区分。
- 旁白单独用 `narrator_voice`，不参与轮转。

## 8. Web 与 CLI 的关系

两者共用 `Pipeline`（`pipeline/runner.py`），没有重复逻辑：

- CLI：`cli.py` 直接 `asyncio.run(pipeline.run_all(...))`，进度打到 stdout。
- Web：`web/app.py` 把同一个 `run_all` 丢进 `asyncio.Task`，进度写进 `jobs` 表，前端轮询。

所以"网页上跑一半关掉浏览器"不会中断任务（服务端还在跑），"重启服务"则会中断，但重启后可以续跑。

## 9. 前端

刻意不引入构建工具：`web/static/` 下三个文件（html/css/js），FastAPI 直接托管。理由：

- NAS 上不需要 Node 运行时；
- `docker build` 一条 `pip install` 就完成，没有 `npm ci` 的几百 MB；
- 单页应用的复杂度本来就不需要框架。

## 10. 数据库表

| 表 | 作用 |
|---|---|
| `books` | 书籍元信息（标题/作者/章节数/字数） |
| `jobs` | 一次任务的状态、阶段、进度、错误信息 |
| `segments` | 每个待合成分片的文本、音色、风格、状态、音频路径 |

`book.json` / `cast.json` 存盘而不是入库，因为它们是"大块 JSON"，读写整存整取更简单，也便于手动查看和修改。
