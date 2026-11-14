# 部署指南

三种方式，按你的情况挑一种。

---

## 方式一：桌面端（最快）

适合：偶尔转一本，机器就是你日常用的电脑。

### 1. 装依赖

```bash
# Python 3.10+
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
```

还需要 **ffmpeg**：

| 系统 | 命令 |
|---|---|
| Windows | `winget install Gyan.FFmpeg`（或去 gyan.dev 下 essentials 包，解压后把 `bin` 加进 PATH） |
| macOS | `brew install ffmpeg` |
| Ubuntu/Debian | `sudo apt install ffmpeg` |

### 2. 启动

```bash
cp .env.example .env      # 填 key
abf serve
```

浏览器打开 `http://127.0.0.1:8848`。

### 3. 后台常驻（可选）

Windows 用 `nssm` 或任务计划程序把 `abf serve` 注册成服务；macOS/Linux 用 `systemd`。

关闭终端/合盖睡眠会中断任务，但重启后 `abf synthesize` 能续跑。

---

## 方式二：NAS + Docker（推荐给长篇）

适合：整本书动不动跑一小时以上，希望关掉电脑也不影响。

### 群晖 DSM 7

1. **装 Container Manager**（套件中心）。
2. **建目录**：在 File Station 里建 `/docker/audiobook/`，把本项目解压进去。
3. **放配置**：把 `.env.example` 复制成 `/docker/audiobook/.env`，填好 key。
4. **建项目**：Container Manager → 项目 → 新增 → 路径选 `/docker/audiobook/docker`，描述文件选 `docker-compose.yml`。
5. 启动后访问 `http://NAS的IP:8848`。

### 威联通 QNAP

App Center 装 Container Station → 应用程序 → 创建 → 粘贴 compose 内容（把 `./data` 改成 `/share/Container/audiobook/data`）。

### Unraid

把项目放到 `/mnt/user/appdata/audiobook/`，用 Compose Manager 插件导入 `docker/docker-compose.yml`。

### 直接用 docker compose

```bash
cd docker
cp ../.env.example ../.env   # 编辑填 key
docker compose up -d --build

docker compose logs -f       # 看日志
docker compose down          # 停止
```

### 数据目录

容器内固定挂到 `/data`，宿主机映射由 compose 里的 volumes 决定：

```yaml
volumes:
  - /volume1/docker/audiobook/data:/data
  - /volume1/docker/audiobook/output:/output
```

`/data` 里会有 `forge.db`、`cache/`、`books/`。**备份就备份这个目录。**

`/output` 是成品目录，挂出来以后可以直接在文件管理器里看到：

```
/output/江湖夜雨/江湖夜雨.mp3
/output/江湖夜雨/江湖夜雨-第01章-雨夜.mp3
/output/江湖夜雨/江湖夜雨.m4b
```

路径由 `ABF_OUTPUT_DIR` 控制（容器内默认 `/output`），想换位置改这个变量再挂载即可。

### 资源限制

NAS 一般 CPU 弱、内存小。建议在 compose 里限制：

```yaml
deploy:
  resources:
    limits:
      cpus: "2.0"      # 2 核足够，瓶颈在网络不在 CPU
      memory: 1G
```

同时把并发调低，避免打满：

```bash
ABF_TTS_CONCURRENCY=2
ABF_LLM_CONCURRENCY=2
```

---

## 方式三：自有服务器 / VPS

步骤同 NAS，注意两点：

1. **别把 8848 直接暴露公网**——接口没有鉴权。用 Nginx/Caddy 反代并加 Basic Auth。
2. 出站要能访问 `api.xiaomimimo.com` 和 `ws://cbm01.cn-huabei-1.xf-yun.com`。

### Nginx 反代示例

```nginx
server {
    listen 443 ssl http2;
    server_name audiobook.example.com;

    ssl_certificate     /etc/letsencrypt/live/audiobook.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/audiobook.example.com/privkey.pem;

    auth_basic           "Audiobook Forge";
    auth_basic_user_file /etc/nginx/.htpasswd;

    client_max_body_size 210m;   # 上传上限 200MB

    location / {
        proxy_pass http://127.0.0.1:8848;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # 长任务轮询，别让代理超时
        proxy_read_timeout 3600s;
    }
}
```

---

## 配置速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `ABF_DATA_DIR` | `./data` | 数据根目录，Docker 里是 `/data` |
| `ABF_OUTPUT_DIR` | `./output` | 成品目录，Docker 里是 `/output` |
| `ABF_HOST` / `ABF_PORT` | `127.0.0.1` / `8848` | 监听地址 |
| `ABF_TTS_PROVIDER` | `mock` | `mock` / `mimo` / `xfyun` |
| `ABF_TTS_CONCURRENCY` | `4` | 同时合成几段。NAS 建议 2，本机可 6~8 |
| `ABF_TTS_RETRIES` | `3` | 单段重试次数 |
| `ABF_SEGMENT_MAX_CHARS` | `120` | 每段最大字数。接口总截断就调小 |
| `ABF_CHAPTER_GAP_MS` | `1200` | 章节间静音 |
| `ABF_BLOCK_GAP_MS` | `250` | 段落间静音 |
| `ABF_FFMPEG` | `ffmpeg` | ffmpeg 可执行文件路径 |

---

## 常见问题

**Q：合成到一半服务挂了怎么办？**
重启后执行 `abf synthesize <book_id>`，已完成的段会跳过（状态在 SQLite 里），只补没做完的。

**Q：如何只重跑失败的分片？**
`abf synthesize <book_id>` 只挑 `pending/failed` 的段。失败的段会把错误写进 `segments.error`，用 `abf books` 或网页"分片明细"能看到。

**Q：能只用 LLM 不用 TTS 吗？**
可以。`abf import` 后 `abf analyze <book_id>`，然后看 `data/books/<id>/book.json` 里标注好的 `type/speaker/style`。

**Q：音色只有几个，能不能自己加？**
可以。MiMo 的 `voice` 支持 `mimo-v2.5-tts-voiceclone` 上传样本做克隆；讯飞在控制台开通发音人权限后用 `ABF_XFYUN_VOICE` 指定 `vcn`。

**Q：Docker 里时区不对？**
compose 里加 `TZ: Asia/Shanghai`，镜像已装 tzdata。

**Q：生成的 mp3 太大？**
调 `ABF_AUDIO_BITRATE=48k`。有声书单声道 48k 基本够用。
