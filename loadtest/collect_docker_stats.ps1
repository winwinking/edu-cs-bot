# 每 5 秒记一次 docker stats（PHASE4.md 4.6"采集"要求）。这一步没法放进 k6 脚本或者容器里跑：
# k6 是 JS 沙箱，没有能力调宿主机的 docker 命令；就算另起一个容器去跑 `docker stats`，
# 也得把宿主机的 docker socket 挂进去，在 Windows + Docker Desktop 上这层转换比直接在宿主机
# PowerShell 里跑一条 `docker stats` 麻烦得多，没必要——本来就是本机压测、本机看数据，
# 直接在宿主机上跑最简单。
#
# 用法（在压测开始前，另开一个 PowerShell 窗口）：
#   powershell -File loadtest/collect_docker_stats.ps1 -OutFile loadtest/output/steady_docker_stats.csv
# 跑完当前场景之后 Ctrl+C 停掉这个脚本，再开始下一个场景前换一个 -OutFile 名字，避免几个场景的
# 数据混进同一个文件里，报告里没法区分是哪个场景的峰值。
#
# 本轮（PHASE4.md 4.6 准备阶段）不执行这个脚本，等 Jo 通知故障注入结束、真正跑压测时才用。

param(
    [Parameter(Mandatory = $true)]
    [string]$OutFile,

    [int]$IntervalSeconds = 5
)

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutFile) | Out-Null

if (-not (Test-Path $OutFile)) {
    "timestamp,container,cpu_percent,mem_usage,mem_percent,net_io,block_io" | Out-File -FilePath $OutFile -Encoding utf8
}

Write-Host "开始每 $IntervalSeconds 秒采集一次 docker stats，写入 $OutFile，Ctrl+C 停止"

while ($true) {
    $timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
    # --no-stream：只采一次快照就退出，不用 docker stats 自带的持续刷新模式，方便按固定间隔控制采样节奏
    $lines = docker stats --no-stream --format "{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}}"
    foreach ($line in $lines) {
        "$timestamp,$line" | Out-File -FilePath $OutFile -Encoding utf8 -Append
    }
    Start-Sleep -Seconds $IntervalSeconds
}
