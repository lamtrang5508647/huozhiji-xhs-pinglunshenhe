#!/usr/bin/env bash
set -euo pipefail

skill_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
agent_id="${1:-main}"
skill_name="comment-review-audit"
destination_root="${HOME}/.openclaw/workspace/skills/${skill_name}"

if ! command -v openclaw >/dev/null 2>&1; then
  echo "同步失败：找不到 openclaw 命令。" >&2
  exit 1
fi

echo "[1/6] 安装 ${skill_name} 到 OpenClaw agent ${agent_id}"
openclaw skills install \
  --agent "${agent_id}" \
  --as "${skill_name}" \
  --force \
  "${skill_root}"

echo "[2/6] 核对已安装文件"
diff -qr -x .openclaw -x __pycache__ "${skill_root}" "${destination_root}"

echo "[3/6] 核对 Skill 可见性"
skill_info="$(openclaw skills info "${skill_name}" --agent "${agent_id}")"
printf '%s\n' "${skill_info}"
grep -q "✓ Ready" <<<"${skill_info}"
grep -q "Visible to model: yes" <<<"${skill_info}"
grep -q "Available as command: yes" <<<"${skill_info}"

echo "[4/6] 核对依赖"
skill_check="$(openclaw skills check --agent "${agent_id}")"
grep -q "Missing requirements: 0" <<<"${skill_check}"
grep -q "comment-review-audit" <<<"${skill_check}"

echo "[5/6] 核对飞书大附件接收上限"
python3 "${skill_root}/scripts/verify_feishu_intake.py"

echo "[6/6] 探测飞书通道（不发送消息）"
channel_status="$(openclaw channels status --probe)"
printf '%s\n' "${channel_status}"
grep -Eq "Feishu .*enabled, configured, running, connected, works" <<<"${channel_status}"

echo "同步完成：${skill_name} 已部署到 ${agent_id}，飞书通道可用。"
