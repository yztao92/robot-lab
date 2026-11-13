# Robot Lab — 本地虚拟机器人

用项目中的真实 STP 外形展示七轴动作、新版屏幕表情和独立 DOA 底座朝向。接收本地 HTTP 事件；不向实际舵机写指令。

## 同事首次使用（macOS）

1. 安装 Git 和 Python 3.10+，浏览器需要支持 WebGL。可从 https://www.python.org/downloads/macos/ 安装 Python；终端执行 `git --version` 确认 Git 可用。无需安装 Node、npm 或 Python 第三方包。
2. 获得私有仓库访问权限后，只需克隆一次：

   ```sh
   git clone https://github.com/yztao92/robot-lab.git
   cd robot-lab
   ```

   也可使用已配置 SSH 的地址 `git@github.com:yztao92/robot-lab.git`。HTTPS 认证使用 Git 凭据管理器或 GitHub CLI，不使用 GitHub 账户密码。
3. 在 Finder 中双击 **启动模拟器.command**，会检查 Python 和运行资源、启动后台服务，并打开浏览器。纯动作、表情预览不需要连接主板或访问互联网。
4. 需要实板数据时，让电脑可以通过网络访问主板，在页面选择或输入 IP，并使用自己的 SSH 凭据。平台不会为新主板安装 Robot 固件，板端需已有兼容服务。

双击入口目前仅支持 macOS，Windows 尚未适配。若 Finder 阻止首次打开，确认仓库来源后使用右键“打开”并按系统提示操作。通过 Git 克隆会保留启动脚本的执行权限。

## 更新与停止

- **更新模拟器.command**：检查本地改动 → `git pull --ff-only` → 检查新资源和 Python 语法 → 重启后台服务 → 打开页面。已有页面请刷新。即使你已手动 pull，仍会重启，确保后端切换到新代码。
- **停止模拟器.command**：停止当前后台服务。保留现有登录自启设置，下次登录仍会自动运行。
- 拉取失败、分支分叉或本地文件有改动时，更新会中止，不主动停止当前服务，也不会自动丢弃改动、stash 或合并。若新资源检查失败，仓库可能已经更新，但尚未重启；修复后重试。
- 重启会清空只存在内存中的主板密码，需要时在页面重新输入。浏览器中未导出的时间线、字幕等请在更新前保存。
- 主板选择保存在 `~/.config/robot-lab/boards.json`；日志保存在 `~/Library/Logs/RobotLab/`，均在仓库外，不随 pull 覆盖。不要把个人凭据放入仓库。
- 同一台 Mac 同时使用一个后台实例；搬动目录或换另一份克隆前，先从原目录停止。不要在外置盘或临时目录中运行需要登录自启的实例。

维护者发布更新：在仓库内完成修改和检查后 `git add`、`git commit`、`git push`。同事在获得仓库权限后双击更新即可。下载 ZIP 可启动，但不支持 Git 更新，建议使用 clone。

## 启动

macOS 推荐双击 `启动模拟器.command`，或在项目根目录执行：

```sh
python3 scripts/local_service.py start
```

浏览器打开 **http://127.0.0.1:8769**。需要 Python 3.10+、支持 WebGL 的浏览器；Three.js 0.186.1 已随项目保存，不依赖 CDN 或 npm 安装。

后台服务由 macOS launchd 管理：关闭终端或 Codex 后继续运行，进程退出后自动拉起。登录 macOS 时自动启动（plist 在 `~/Library/LaunchAgents/com.yztao92.robot-lab.plist`）。重复启动会复用现有服务。停止请双击 `停止模拟器.command`，或执行 `python3 scripts/local_service.py stop`；也支持 `restart`、`status`。修改 Python 服务代码后使用 `restart`。

日志位于 `~/Library/Logs/RobotLab/stdout.log` 与 `stderr.log`；服务定义位于 `~/Library/LaunchAgents/com.yztao92.robot-lab.plist`。项目搬家后先停止，再从新目录启动，会更新绝对路径。需要仅启动本地页面时用 `start --no-board`（已运行时用 `restart --no-board`）；之后仍可在页面选板。

前台调试可用 `python3 server.py --port 8769`，此方式依附终端、Ctrl+C 停止。不要与后台服务同时占用相同端口。

若端口已被占用，可用 `--port 8770` 并在浏览器使用相同端口。不要用 `file://` 直接打开 HTML。

若页面提示本地服务不可用或资源未加载，按页面中的启动说明恢复服务，再点击“重新加载”。

## 操作

- **动作表情**：21 套动作、23 套表情、16 个记录/翻译状态。选中后点击播放；表情可完整演绎或循环保持。
- **关节调试**：七轴角度均相对中位，左右按机器人自身。滑块会中断正在执行的动作，并从当前姿态运动；连续调节多个关节会保留各轴尚未到达的手动目标。
- **DOA 底座**：0° 是预览正前方，左转为正，输入范围 `[0,360)`。仅底座朝向使用跨零最短路径；手臂不做角度环绕。
- **信号接入**：注入 JSON，检查时间线中的输入和模拟执行回执。表单强制标记 `source=injected`。
- **停止**：保留当前七轴、底座与屏幕画面；**七轴归中**只恢复七轴与待机表情；**恢复初始状态**同时将底座朝向归零。
- **暂停**暂停七轴和屏幕；DOA 继续独立更新。预览速度同时作用于七轴、底座和视频。页面隐藏时预览时间与视频暂停。

## 原始资料与执行规则

| 用途 | 原始资料 |
|---|---|
| 外形 | `../STP&CMF/00-id-movi_20260731.stp` |
| 材质 | `../STP&CMF/机器人参展_CMF.pdf` |
| 表情与动作说明 | `../表情&动作/展示.html` |
| 动作目标 | `../表情&动作/动作数据.csv` |
| 协议速度、使能、方向与阶段 | `data/ServoActionList.json`（原文件副本） |
| 屏幕素材 | `media/`，62 个原始 MP4 |

运行时只读取本仓库文件，不依赖上表的外部原始资料。不使用旧 HTML 的机身视频或旧工业外形。`data/catalog.json` 提取自新版 HTML，`assets/robot-mesh.json` 为预转换网格。维护者更新素材时运行 `python3 scripts/prepare_assets.py --source "/原始资料/表情&动作"`，会重新生成目录并复制原始协议、MP4；随后提交到 Git。同事无需执行素材生成。

动作按协议的 70 个阶段播放：21 个中位初始化 + 49 个设计目标。`en=0` 保持当前姿态；全部启用轴模拟到位后才计入 `hold_ms`。动作不会控制槽位 3 的 DOA 底座。协议的头部侧倾、左臂旋转方向转换为 HTML 的语义方向。

进入动作播放配对表情的完整片段，随后切换为其循环片段；动作完成后恢复 D01 待机。新动作从当前姿态先归中。

**收到的表情和动作按顺序播放，不互相打断。** 应用侧发得比播得快，所以预览排了一个队：

- **排队项**：`action`、`full`/`once` 表情、带 `action_id` 的表情。上一项的动作走完、表情片段也完整播过一遍，才开始下一项。片段播完后会接着循环，直到下一项开始。
- **保持类画面**：`state`、不带动作的 `loop` 表情。它们不进队列，只记最新一个，等队列清空后显示；队列为空时立即显示。
- **合并与上限**：和正在播放的项或队尾相同的项会合并；最多排 6 项，再来就丢掉最早排队的那项。
- **兜底**：动作走完后，如果表情 20 秒还没报告播完，就跳过它，避免卡住队列。
- **清空队列**：停止、归中、断线或 `gap`，以及手动播放、手动调关节，都会清空队列。
- **看状态**：画面下方「情绪」格显示正在播放和排队的情况，运行日志里的「队列 · …」是每一次排队、合并和丢弃的记录。

这套协调规则是本原型的可修改策略，不代表固件里已有这样的逻辑。

## 接入事件

服务默认仅监听 `127.0.0.1`。每个 POST 提交一个 JSON 对象，`Content-Type: application/json`。GET `/api/events` 是 SSE；GET `/api/health` 可查看本地接收端数量。POST 成功只代表桥接收到，不是浏览器执行完成。未打开预览时不缓存动作命令。

```sh
curl http://127.0.0.1:8769/api/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"action","source":"injected","action_id":"A02"}'
```

| type | 必填字段 | 示例 |
|---|---|---|
| `action` | `action_id` | `"A02"`（A01–A21） |
| `expression` | `expression_id`, `mode` | `"F19"`, `"loop"`；支持 F01–F21、D01/D02；模式 full/loop/once（once：播一次后回 D01） |
| `state` | `state_id` | `"T03"`；支持 R00–R07、T01–T08 |
| `doa` | `angle_deg`, `frame` | `120`, `"world"` |
| `stop` | 无 | 保留当前画面与姿态 |
| `reset` | 无 | 七轴归中、底座归零、待机表情 |

所有事件还需 `source`：`injected` / `replay` / `board` / `cloud` / `pc`；可选 `timestamp` 为带时区 ISO 8601 字符串。未知字段、未知编号、超范围或非有限角度会被拒绝。source 由发送方声明，不证明来源认证或硬件完成。

DOA 采用**世界坐标绝对朝向**。板上若输出相对麦克风阵列的方位角，适配器需要结合当前底座朝向、传感器安装偏角及正负方向换算，不能直接把相对声源角当绝对目标。预览底座速度暂设 60°/s，不是底座原生速度字 `190` 的已验证换算。

服务按接收顺序分配 `seq`。断线、接收溢出 `gap` 会停止预览；恢复后由生产者重发最新状态，不自动重放旧动作。除上面的表情/动作队列合并外，尚无事件去重、跨端时钟对齐、硬件执行回执或持续状态重建。多个预览页各自独立执行接收到的事件。

## 连接实板

macOS 页面左上角「主板」可扫描局域网或手动输入 IP；服务经 ssh（root，密钥优先，必要时弹窗要密码，只存内存）连上所选板子，并在板上用系统 python3 运行 `scripts/board_agent.py`（只读，不安装、不写文件）。适配的是 `code/robot`（Hui 的仓库，eab4481）在 RK3576 上的新栈：

| 板端 | Lab 里看到的 |
|---|---|
| voiced 日志 | 字幕、对话阶段、DOA、服务端 / 意图耗时；安静后「待唤醒」，`woken` 时 D02 播一次 |
| waked 日志 | 唤醒词命中（运行日志） |
| translated 日志 | 同传原文 / 译文字幕；状态屏 T01–T03（中→英）或 T05–T07（其他→中） |
| mediad 日志（face.rs） | 板上屏幕实际播放的情绪：18 种 → F 系列，播一次后回 D01（`mode: "once"`），**同时播配对动作 Fnn ↔ Ann**（按设计稿演示；真板子上 mediad 只播表情、robotd 是 fake，不会动），和其他表情动作一样进队列。mediad 在跑时，对话阶段不再自己换表情，与板子屏幕一致 |
| robotd `robot.state` | 头部姿态（未标定） |
| systemd / `/proc` / debugfs | 六个服务状态、温度、CPU 占用（8 核合计忙碌比例，读两次 `/proc/stat` 求差；1 分钟负载放在悬停提示里）、NPU 负载（`/sys/kernel/debug/rknpu/load` 各核平均，一瞬间的值）、过热降频时的 CPU 频率上限、音量、亮度、风扇 |

**性能面板**仅显示首词、服务端、意图、DOA四项，统一以毫秒显示，只统计首词与服务端耗时均已到齐且首词 ≥ 服务端的有效轮次，再按各项已有数值计算中位数、P90和有效轮数。首词 < 服务端时整轮作废，原始行保留并标注“无效”；未到齐标“待校验”，暂不计入统计。

- 首词直接采用 voiced 的 `reply audible after the user's last word` 日志 `e2e_ms`，其中已包含 ALSA 队列时间；这是估计尾音到预计开始播放的耗时，不是外部声学实测首字。
- 服务端采用 `first reply audio after the user finished` 的 `ms`；意图采用 `intent L2` 的 `ms`。
- DOA = 板端收到云端 ASR_INFO 的日志时刻 → 同轮 user 日志发布用于转向的 DOA 方向时刻，两端均使用板端日志时间，包含说话时长及判停，不含后续SSH/SSE/浏览器转发及电机运动。开始事件采用 `user started speaking`（DEBUG）或 `user talked over the reply; cutting it`（INFO）；缺起点/角度、时间倒序或连接跨会话时不填数。需会话模块日志 `RUST_LOG=info,voiced::session=debug`，不新增音频采集或定位算法。
- Lab不再额外读取麦克风或功放回采来计算首词；旧采样结果也不会覆盖日志数值。唤醒提示音不计作当前语句的回复。

**画面布局**：情绪、动作、DOA 和七轴姿态在宽屏（超过 1400 px）时浮在机器人画面左下角，窄一些时放在画面下方。

**内存**：「系统」里的 MEM 是已用比例（`MemTotal` 减 `MemAvailable`），不算可回收的缓存，所以比 `free` 的 used 更接近真实压力；达到 90% 变色提醒。

**服务 CPU**：机器人画面左上角显示 6 个服务各自的 CPU 占比，读的是板上 cgroup 的 `cpu.stat`（含子进程），约 5 秒更新。单位和「系统」里的 CPU 相同，是占全部 8 个核的比例。服务没运行时显示红色 ✗。

**DOA 已镜像**：voiced 的 `doa_deg` 在板上与 Lab 底座（机器人左转为正）左右相反，桥接里统一取反（`board_bridge.DOA_MIRROR`）。手动滑块和注入事件的约定不变。

**摄像头窗口**：顶栏「摄像头」打开浮窗，1920×1080（传感器当前输出的原画），目标 4 fps。visiond 占着 ISP 主输出 `/dev/video11` 且只发事件不给画面，所以 `scripts/board_camera.py` 读 ISP 的另一路输出 `/dev/video12`，visiond 继续运行，人脸 / 人数事件不中断，脚本也不碰传感器参数。`code/robot/docs/project/media-bringup.md` 记录过早期原型上并行读这一路会让采集管线挂掉；2026-11-02 在 0.7 号板上试过，visiond 没有报错也没有重启。标题栏「旋转」只改变浏览器显示，角度保存在浏览器。关闭窗口、关页面、15 秒没有页面取帧、切换主板、Lab 退出或网络断开（心跳停止）时，板端脚本都会退出。

原来的做法留作备用（`--device /dev/video11`）：看画面期间停掉 visiond，退出时再启动。重启 visiond 时摄像头和 NPU 一起初始化，这个峰值在 0.7 号板上引起过输入掉电（RK806 `OFF: 0x0`）。

画面是 zlib 压缩的原始 NV12，1080p 每帧约 740 KB。2026-11-04 在手机热点 Wi-Fi 上实测约 2 fps、12 Mbps，板端脚本约占 60% 个核。

窗口里的「点位」按钮（默认开）把 visiond 检测到的人脸框和 98 个关键点叠在画面上，框变橙色表示在说话，跟着「旋转」一起转，超过 1.5 秒没更新就不画。这需要 visiond 在 `Faces` 事件里发 `bbox` 和 `landmarks`：`code/robot` 本地分支 `lab/face-landmarks` 才有，官方版本的 visiond 不发，窗口就只有画面。0.7 号板的摄像头是倒装的，人脸模型要看到转正的画面才检测得好，所以板上 visiond 启动参数加了 `--camera-upside-down`（同样只在这个分支有），它发出来的坐标仍是摄像头原始画面上的位置，窗口不用改。部署脚本会重写这个启动参数。画面按 BT.601 全范围解码，ISP 报告的是 `Quantization: Full Range`。

**不要 `systemctl restart visiond`**：停了马上启动，3A（`rkaiq_3A_server`）还没回到等待状态就错过视频流启动事件，之后没有自动曝光和白平衡，画面又暗又绿（传感器停在默认的曝光 235、增益 0）。要重启就先 `stop`，等约 6 秒再 `start`。

板端写操作仅有：`systemctl start|stop|restart voiced`、音量、亮度、风扇。从不发舵机指令。

## 示例回放

```sh
python3 scripts/send_events.py --dry-run
python3 scripts/send_events.py --interval 2
```

默认文件 `examples/events.jsonl`；强制标记 replay，按间隔发送，不等待上条动作完成；预览端会把来不及播的表情和动作排队。先打开预览，工具会检查是否有接收端。时间线导出文件含模拟回执，仅供诊断，不能直接作为命令回放文件。

## 几何与验证边界

- `config/rig.json` 配置头部/肩部轴心、层级、底座旋转分组与 CMF 材质。当前轴心基于几何推定，全部标记为待标定。中位保留 STP 装配位置；屏幕保持视频比例，实际有效显示区待确认。
- 银色环目前为一个 CAD 网格，不能在现有分件中独立分配蓝色盖件；屏幕 RGB 配色不等于经色彩标定的 CMF 样品。
- 本原型是运动学预览，不含碰撞/力矩/负载/机械限位实测；模拟到位不是电机到位。
- 电脑模型转头不会转动实板上的固定摄像头，不能用它验收真实视觉跟随闭环。
- 浏览器画面和程序测试均不代替机械标定或实板联调；实际验收记录见知识库 `projects/robot-hardware/`。

## 程序检查

在项目根目录执行：

```sh
node --test tests/*.test.mjs
python3 -m unittest discover -s tests -p 'test_*.py'
```

覆盖动作时序与中断、方向与范围、底座隔离、STP 中位坐标、屏面贴图条件、HTTP/SSE、视频范围读取及输入校验。

进度与决策唯一记录位置：`~/knowledge/projects/robot-hardware/`。此文件仅为本地交付物使用说明。
