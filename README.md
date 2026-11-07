# heibox-history

把**自己**小黑盒主页的帖子缓存到本地，然后按任意时间范围查询。

小黑盒主页只提供无限滚动，没法指定"查 2024 年 3 月"。但列表接口本身支持
`offset` 分页，所以本地建一份索引即可：一次全量通常只要十几个请求，之后查询
全部走 SQLite，不再打扰接口。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

需要 Python 3.11+（开发和验证用的是 3.13）。

## 图形界面

不想开命令行就双击 `dist\heibox-history.exe`（单文件，约 14 MB，绿色免安装）。
改代码时也可以直接跑：

```powershell
heibox-gui
# 指定数据目录（默认与命令行共用 %LOCALAPPDATA%\heibox-history）
heibox-gui --data-dir .\data
```

窗口只做三件事：

- **按时间范围查询**：填 `2024` / `2024-03` / `2024-03-05`，或点「今年 / 去年 / 全部」，
  回车或点「查询」。查询只读本地 SQLite，**不联网**。结果列：时间 / 标题 / 摘要 / 浏览 / 点赞 / 评论。
- **手动同步**：点「同步最新」刷最近几页，「补全更早」继续往前翻历史，「刷新全部」从第一页
  重扫一遍（用于给老数据补齐新字段，比如点赞数）。不点就不发请求，平时是静态的。
  撞上人机验证会保留断点并提示，扫码登录后继续。
- **扫码登录**：二维码直接画在窗口里，用小黑盒 App 扫即可，Cookie 与命令行共用。

双击结果行会在浏览器打开原文。

> 点赞数取的是接口里的 `link_award_num`（网页版 👍 那个数字）。加这一列之前抓的旧数据
> 会显示 `—`，点一次「刷新全部」就补齐了。

## 命令行用法

```powershell
# 绑定账号（userid 是个人主页网址末尾的数字）
heibox init --userid 20475763

# 扫码登录（可选；接口要求人机验证时再用，登录态能少踩风控）
heibox login

# 首次全量：从最新一页翻到最早一页，中断可续
heibox sync --full

# 条数比接口报告的少时，从头重扫一遍（列表会临时过滤少量条目）
heibox sync --rewalk

# 按范围查询；--from/--to 支持 2024、2024-03、2024-03-05
heibox list --from 2024-01-01 --to 2024-03-31

# 精确到某个期间（整月/整年），避免单边 --from 的开区间歧义
heibox list --period 2024-03

# 输出 JSON（stdout 只有 JSON，方便管道）
heibox list --period 2024-03 --json

# 只看本地缓存，不联网
heibox list --from 2024 --no-sync

# 单条详情 / 在浏览器打开
heibox show 195926115
heibox open 195926115

# 缓存覆盖情况
heibox status
```

## 数据位置

默认 `%LOCALAPPDATA%\heibox-history\`（`config.toml` + `heibox.db`）。
想把数据放在项目里：

```powershell
heibox --data-dir .\data init --userid 20475763
heibox --data-dir .\data sync --full
```

全局参数 `--data-dir` 要放在子命令前面。

## 行为约定

- **按需回填**：查询范围早于本地缓存时，会自动从断点继续向更早翻页，直到覆盖查询起点或到达账号第一帖。
- **增量刷新**：查询范围包含"最近"且距上次同步超过 10 分钟时，先刷新最新几页；`--no-sync` 可完全离线。
- **空结果有区分**："本地没缓存"、"范围早于最早缓存"、"范围内确实没发帖" 是三种不同提示。
- **未来时间**截断到当前时间，不报错。
- **时间**统一按 UTC+8 解释和展示，存库为 Unix 秒。

## 接口备注

- 路径：`/bbs/app/profile/user/link/list`，参数 `userid` / `offset` / `limit`；
- `limit` 上限 60，单页返回条数可能略少于 `limit`，因此翻页步长必须用 `limit`，不能用实际返回条数；
- 当前无需登录态，但这是未公开接口，随时可能加签名或改结构；相关逻辑集中在 `src/heibox/api.py`。

## 遇到"请先验证"（show_captcha）

请求过于集中时，接口会返回 `status=show_captcha`。程序会保留已抓取的部分并以退出码 4 结束，
重新运行会从断点继续：

1. 扫码登录（推荐）：

   ```powershell
   heibox login
   ```

   终端会显示二维码，用小黑盒 App 扫码确认即可。成功后 Cookie 会保存，并自动校验
   登录账号与配置的 userid 是否一致（不一致会丢弃 Cookie，避免抓到别人主页）。

2. 或者等 10~30 分钟冷却，直接 `heibox sync --full`；
3. 或者手动保存浏览器里的 Cookie：

   ```powershell
   heibox auth            # 打开主页，验证后从 DevTools 复制 Cookie，粘贴（不回显）
   heibox auth --file .\cookie.txt
   heibox auth --clear
   ```

Cookie 会以明文保存在 `config.toml`，等同于一份会话凭证，不要提交到版本库或发给别人。
环境变量 `HEIBOX_COOKIE` 优先级更高，适合临时使用。

同步默认每 3 秒左右请求一次。全量约 14 页，建议一次跑完就停，不要反复重跑。

## 打包成 EXE

```powershell
.\build.ps1            # 增量构建
.\build.ps1 -Clean     # 清掉 build/ dist/ 重打
```

产物 `dist\heibox-history.exe`：单文件、无控制台、约 14 MB。

体积基本盘是 CPython 运行时（约 8 MB）和 Tcl/Tk（约 5 MB），所以界面用的是标准库
`tkinter` 而不是 Qt / Web 方案——同样功能的 PySide6 包要到 60 MB 以上。
`heibox-history.spec` 里已经把 `rich`、`typer` 等只服务命令行的依赖排除；加新依赖前先
`build.ps1 -Clean` 跑一次，确认没有把体积顶上去。

## 注意

只用于抓取自己的账号，保持默认的低频请求。不要改成批量抓取他人主页。
