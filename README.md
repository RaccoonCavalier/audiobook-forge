# Audiobook Forge · 有声书工坊

把 **EPUB / TXT / Markdown** 变成**多角色有声书**：LLM 负责"读懂"和"导演"，TTS 负责发声。

一句话概括流程：

```
文本文件 → 解析归一 → LLM 分拣(角色/对白/情绪) → 分段 → TTS 并发合成 → ffmpeg 拼接 → mp3 / m4b
```

---

## 为什么这么设计

**纯 TTS 工具的通病**：整本书用同一个音色平铺直叙地念完，对话和旁白没有区分，听十分钟就困。

**本项目的做法**是把"朗读"拆成两件事：

| 环节 | 交给谁 | 产出 |
|---|---|---|
| 谁在说话、什么情绪、哪句不该读 | LLM（任意 OpenAI 兼容模型） | 每个段落的 `type / speaker / style` |
| 把文字变成声音 | TTS（小米 MiMo / 讯飞 / 本地 mock） | 音频分片 |

这样做的三个好处：

1. **角色能区分**：旁白一个音色，张三一个音色，李四另一个音色。
2. **情绪能落地**：MiMo 支持自然语言风格描述（"冰冷 压低声线"），直接写进 `style` 字段。
3. **成本可控**：LLM 只读文本（便宜），只有需要发声的段落才调 TTS。

---

## 快速开始

```bash
# 1. 安装（Python 3.10+，需要 ffmpeg 在 PATH 里）
pip install -e .

# 2. 配置
cp .env.example .env
#    编辑 .env，至少填一个 TTS 的 key；不填也能用 mock 跑通全流程

# 3. 体检
abf doctor

# 4. 起 Web 界面
abf serve            # → http://127.0.0.1:8848
```

或者纯命令行一步到位：

```bash
abf build 我的小说.epub --provider mimo
```

产物在 `data/books/<id>/out/` 下：`书名.mp3` 和 `书名.m4b`（带章节标记）。

---

## 两种部署形态：桌面 还是 NAS？

这是本项目最重要的一个设计取舍。**代码完全一样**，区别只在"在哪跑、跑多久"。

### 先算一笔账

中文朗读速度约 **5.2 字/秒**，所以：

| 原文字数 | 音频时长 | 相当于 |
|---|---|---|
| 1 万字（短篇） | ≈ 0.5 小时 | 半集播客 |
| 10 万字（普通长篇） | ≈ 5.3 小时 | 一整季 |
| 30 万字（网文） | ≈ 16 小时 | 一天听不完 |

合成耗时 ≈ 音频时长 × RTF ÷ 并发数，再叠加 LLM 分拣和转码：

| 环节 | 10 万字大概耗时 | 说明 |
|---|---|---|
| LLM 分拣 | 15 – 40 分钟 | 取决于模型速度，并发 3 路 |
| TTS 合成 | 25 – 60 分钟 | 4 并发，MiMo/讯飞实际水平 |
| 转码 + 拼接 | 2 – 5 分钟 | ffmpeg，本地 CPU |
| **合计** | **约 45 – 105 分钟** | 一本书 |

### 结论

| 场景 | 推荐 | 理由 |
|---|---|---|
| 偶尔转一两本短篇 | **桌面端** `abf serve` | 开箱即用，不用折腾 Docker |
| 整本长篇、网文、批量 | **NAS（Docker）** | 常开低功耗，跑一小时也无所谓，关机不影响 |
| 想让家里其他人也能用 | **NAS** | 手机/平板浏览器直接上传、试听 |

关键点：**耗时长短不影响正确性**。所有分片都落库，进程随时可以杀掉，下次 `abf synthesize` 会自动跳过已完成的部分，只补没做完的。所以"很久"这件事本身不可怕，怕的是"跑到一半断了要重来"——这个问题已经解决了。

详细步骤见 [docs/DEPLOY.md](docs/DEPLOY.md)。

---

## 配置要点

两种方式，优先级从高到低：

1. **Web 界面 → 右上角「⚙ 设置」**：切换 TTS 提供商、开关 LLM、填地址/模型/密钥。改完立刻生效（下一个任务），存在 `data/settings.json`，重启后仍保留。
2. **环境变量**（`.env` 或 docker compose 注入），完整清单见 `.env.example`。

> `data/settings.json` 优先级高于 `.env`。想回到纯环境变量管理，删掉这个文件即可。密钥只存在 `data/` 下，`.gitignore` 与 `.dockerignore` 都已排除。

### TTS 提供商

| provider | 需要配置 | 特点 |
|---|---|---|
| `mock` | 无 | 本地合成正弦波，用于跑通流程、写测试 |
| `mimo` | `ABF_MIMO_API_KEY` | 小米 MiMo TTS，支持自然语言风格、方言、角色扮演；当前限时免费 |
| `xfyun` | `ABF_XFYUN_APP_ID` + 密码或密钥对 | 讯飞超拟人合成，发音人极多（`x6_*` 系列） |

### LLM

任何 OpenAI 兼容端点，界面里有三个预设：

| 预设 | 地址 | 模型 |
|---|---|---|
| SiliconFlow · DeepSeek V3.2 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3.2` |
| DeepSeek 官方 | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 小米 MiMo | `https://api.xiaomimimo.com/v1` | `mimo-v2.6-flash` |

也可以写成环境变量：

```bash
ABF_LLM_BASE_URL=https://api.siliconflow.cn/v1
ABF_LLM_API_KEY=sk-xxx
ABF_LLM_MODEL=deepseek-ai/DeepSeek-V3.2
```

不配也能跑（`ABF_LLM_ENABLED=false`），但会退化成纯正则分拣：只能认出"张三说道"这种标准句式，复杂对话会标成"未知角色"，朗读指令也基本为空。

---

## 常用命令

```bash
abf doctor                    # 环境体检：Python/ffmpeg/各 provider 是否就绪
abf status                    # 当前配置与提供商状态
abf import 书.epub            # 只解析，看看章节和字数
abf books                     # 书架列表与进度
abf analyze <book_id>         # 重新分析（角色/对白/情绪）
abf synthesize <book_id>      # 合成音频（可反复执行，自动续传）
abf synthesize <book_id> --chapters 1,3,5-7   # 只合成指定章节
abf synthesize <book_id> --single-voice        # 全书一个音色（轻小说推荐）
abf synthesize <book_id> --single-voice 冰糖   # 指定用哪个音色
abf assemble <book_id>        # 拼成整本 mp3 / m4b
abf build 书.txt --provider mimo   # 一步到位
abf build 书.txt --chapters 1-3    # 先做前 3 章试听效果
abf serve                     # Web 界面
```

Web 界面上可以：拖拽上传、看进度条、**逐段试听**、看识别出的角色表、下载成品。

两个和"钱和时间"直接相关的功能：

- **按章合成**：章节表格里勾选想要的章，点「合成选中章节」。适合先做一章听听效果，或者只补某一章。每章后面显示 `已合成/总数`，点「只选未合成」可一键挑出没做完的章。
- **随时中止**：任务进度条右边有「中止」。已完成的分片会保留、状态不会丢，下次继续时只补剩下的部分；中止发生在拼接阶段也不会留下半截文件（成品先写临时文件再原子替换）。

还有一个**单声线模式**（操作区「单声线（全书一个音色）」勾选框）：

日系轻小说这类文本，对话常常没有"某某说道"的标记，靠语气和上下文区分说话人。自动判角色一旦判错，就会出现"旁白男声、下一句突然变女声"的跳戏感。勾上单声线后，旁白、对白、标题全部用同一个音色，靠朗读情绪（style）而不是换声来区分场景，听感稳定得多。可以在旁边下拉框指定用哪个音色，选择会记住。

> 声线模式是写进分段的，切换后会自动重新分段（这是必须的），所以之前合成好的音频不会复用。同理，多角色听腻了想换单声线，等于重跑一遍。

---

## 目录结构

```
src/audiobook_forge/
├── ingest/         EPUB / TXT / Markdown 解析
├── textproc/       清洗、分句、按 TTS 长度切段
├── llm/            提示词、OpenAI 兼容客户端、选角与标注编排
├── tts/            抽象层 + mimo / xfyun / mock 三个实现
├── pipeline/       分段计划、并发合成（缓存/重试）、ffmpeg 拼接、总编排
├── web/            FastAPI + 单页前端（零构建）
└── cli.py          命令行入口
```

运行数据（不入库）：

```
data/
├── forge.db            任务与分片状态
├── cache/              按文本 hash 缓存，重跑不重复付费
└── books/<id>/
    ├── book.json       解析+标注后的结构化书
    ├── cast.json       角色 → 音色
    ├── audio/          分片 mp3
    └── out/            成品 + manifest.json
```

---

## 已知限制

- **多角色识别依赖 LLM**。没配 LLM 时，只能靠"X说"这类句式，覆盖率有限。
- **方言/角色扮演**只有 MiMo 支持（讯飞靠发音人 + 语速语调参数近似）。
- **m4b 章节**按分片时长累加，与音频实际时间可能有几百毫秒误差。
- EPUB 只处理 `spine` 里的 XHTML；DRM 加密的书不支持。
- 长文本没有做"跨段上下文"的指代消解，说话人换了称呼（"张三"→"三哥"）可能分到不同音色。

---

## 许可

MIT
