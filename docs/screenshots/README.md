# 演示截图规范

公开截图的可重复源页面位于 [`../demo/`](../demo/index.html)，由
[`../../scripts/build_demo.py`](../../scripts/build_demo.py) 生成。目前固定提供：

- [`teacher-status.html`](../demo/teacher-status.html)：教师状态总览；
- [`student-login.html`](../demo/student-login.html)：无预填凭据、不可提交的学生登录页；
- [`collection-report.html`](../demo/collection-report.html)：使用合成 RID 的收卷完成报告。

页面不依赖生产数据库、服务器或网络。修改生成器后必须重新生成并检查：

```powershell
python scripts/build_demo.py
python scripts/build_demo.py --check
python -m unittest orchestrator.tests.test_demo_assets
```

## 固定视口截图

在仓库根目录用已经安装的 Chrome 或 Edge 直接打开本地 HTML 并生成真实 PNG。每个页面
使用独立的临时浏览器配置，等待文件真正落盘后才继续，并在结束时清理临时目录。命令会在
找不到本机浏览器或渲染失败时明确报错，不会生成占位图片：

```powershell
$candidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
$browser = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $browser) { throw "未找到本机 Chrome 或 Edge；只保留可复现 HTML，不生成 PNG。" }
$output = (Resolve-Path docs/screenshots).Path
$pages = @('teacher-status', 'student-login', 'collection-report')
foreach ($page in $pages) {
  $source = (Resolve-Path "docs/demo/$page.html").Path
  $url = ([System.Uri]::new($source)).AbsoluteUri
  $png = "$output\$page.png"
  $profile = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("noi-demo-browser-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $profile | Out-Null
  try {
    if (Test-Path -LiteralPath $png) { Remove-Item -LiteralPath $png -Force }
    & $browser --headless=new --no-sandbox --disable-gpu --disable-gpu-compositing `
      --disable-software-rasterizer --hide-scrollbars --force-device-scale-factor=1 `
      --disable-background-networking --disable-component-update --disable-sync `
      --no-first-run --no-default-browser-check "--user-data-dir=$profile" `
      --window-size=1440,900 "--screenshot=$png" $url
    $ready = $false
    foreach ($attempt in 1..80) {
      if ((Test-Path -LiteralPath $png) -and (Get-Item -LiteralPath $png).Length -gt 10000) {
        $ready = $true
        break
      }
      Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw "截图未成功生成：$page" }
  }
  finally {
    Start-Sleep -Seconds 1
    Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
  }
}
```

生成后逐张人工检查，确认页面完整且没有浏览器错误页；随后再执行文本扫描和必要的
OCR 审核。PNG 只有在上述命令实际成功后才能加入仓库。

公开 README 计划使用以下六类真实界面截图：

1. 只读安装 Doctor；
2. 教师办赛向导与材料审核；
3. 座位池全部通过验收；
4. 学生 NOI Linux 桌面；
5. CSP 程序回收页面；
6. 收卷与 Hydro RID 报告。

截图必须来自真实 UI 和固定的脱敏演示数据，不能使用生产比赛直接截图。

## 禁止出现

- 真实域名、IP、实例 ID、安全组 ID、TID 和用户 UID；
- gateway token、VNC 密码、准考证号、Cookie 和管理员凭据；
- 学生姓名、源码、隐藏数据、答案和真实收卷路径；
- 云账单、AccessKey、SSH 主机指纹和内部拓扑。

## 生成原则

- 演示数据使用保留域名 `example.test` 和文档地址段。
- 页面宽度、主题和数据固定，截图由脚本重复生成。
- README 首屏使用短 GIF，下面使用六张带说明的静态图。
- 每张图生成后都要进行 OCR/文本扫描，确认没有秘密和站点专用信息。
- 截图脚本、演示数据和最终图片与对应 Git tag 一起发布。
