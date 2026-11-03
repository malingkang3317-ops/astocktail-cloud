#!/usr/bin/env bash
# Install two system-level timers for an existing Ubuntu repository and .venv.
# Does not change strategy.py, existing cron entries or existing services.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo '请使用 sudo bash install_observer.sh /home/ubuntu/astocktail-cloud ubuntu [通知环境文件]'
    exit 1
fi
repo_dir=$(realpath "${1:-.}")
run_user=${2:-${SUDO_USER:-ubuntu}}
env_file=${3:-"$repo_dir/observer.env"}
if [[ ! "$repo_dir" =~ ^/[A-Za-z0-9_./-]+$ ]] || [[ ! "$env_file" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
    echo '请使用不含空格或特殊字符的绝对路径'; exit 1
fi
id "$run_user" >/dev/null
if [[ ! -x "$repo_dir/.venv/bin/python" || ! -f "$repo_dir/astocktail_observer.py" ]]; then
    echo '未找到项目 .venv/bin/python 或 astocktail_observer.py'; exit 1
fi
"$repo_dir/.venv/bin/python" -c 'import sys, eltdx; assert sys.version_info >= (3,11)'
install -d -m 0755 -o "$run_user" -g "$(id -gn "$run_user")" "$repo_dir/observer_data"
if [[ ! -e "$env_file" ]]; then
    install -m 0600 -o "$run_user" -g "$(id -gn "$run_user")" /dev/null "$env_file"
    echo '# 在这里填写现有Server酱密钥；不要提交此文件到GitHub' > "$env_file"
    echo 'SERVERCHAN_SENDKEY=' >> "$env_file"
    echo "已生成通知配置模板：$env_file；未填写密钥时仍运行，只记录通知缺失。"
fi
# Prevent accidental git add of credentials and potentially large local records.
if [[ -d "$repo_dir/.git" ]]; then
    exclude="$repo_dir/.git/info/exclude"
    touch "$exclude"
    for entry in '/observer.env' '/observer_data/'; do
        if ! grep -Fxq "$entry" "$exclude"; then echo "$entry" >> "$exclude"; fi
    done
fi
for task in run update; do
    extra=''
    if [[ "$task" == run ]]; then extra='--notify'; fi
    cat > "/etc/systemd/system/astocktail-observer-$task.service" <<EOF
[Unit]
Description=AStockTail rank observer $task
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$run_user
WorkingDirectory=$repo_dir
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=$repo_dir/.venv/bin/python -u $repo_dir/astocktail_observer.py $task --root $repo_dir --env-file $env_file $extra
TimeoutStartSec=20min
KillMode=control-group
NoNewPrivileges=true
UMask=0077
EOF
 done
cat > /etc/systemd/system/astocktail-observer-run.timer <<'EOF'
[Unit]
Description=AStockTail rank observer weekdays 14:39:30 China
[Timer]
OnCalendar=Mon..Fri *-*-* 14:39:30 Asia/Shanghai
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit=astocktail-observer-run.service
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/astocktail-observer-update.timer <<'EOF'
[Unit]
Description=AStockTail observer outcome refresh after close
[Timer]
OnCalendar=Mon..Fri *-*-* 15:20:00 Asia/Shanghai
OnCalendar=Mon..Fri *-*-* 16:20:00 Asia/Shanghai
AccuracySec=5s
RandomizedDelaySec=0
Persistent=false
Unit=astocktail-observer-update.service
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now astocktail-observer-run.timer astocktail-observer-update.timer
systemctl list-timers --all 'astocktail-observer-*' --no-pager
printf '\n安装完成。启动的是定时器，没有立即运行选股或发送消息。\n'
printf '北京时间14:39:30启动，14:40/14:45/14:49预选，14:50复核；韩国时间加1小时。\n'
printf '原有任务没有自动停用；如原任务也发通知，请停用对应旧任务以避免重复。\n'
printf '查看日志：journalctl -u astocktail-observer-run.service -n 100 --no-pager\n'
printf '停止新任务：sudo systemctl disable --now astocktail-observer-run.timer astocktail-observer-update.timer\n'
